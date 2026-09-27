"""Recompute market indicators after a completed daily run."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from runner import Drive, compact, digest, load_market, now_myt, parse_lines_gz
from foundation import current_universe, daily_segments
from analytics import compose, excursions, indicators, split_adjust


def calculate_anchors(rows, anchors):
    result = []
    for anchor in anchors:
        args = {key: value for key, value in anchor.items()
                if key in ("signal_date", "entry_date", "entry_price", "direction")}
        try:
            result.append(excursions(rows, **args))
        except ValueError as exc:
            result.append({"anchor": args, "status": str(exc)})
    return result


def read_files(drive: Drive, market: str, folder: str, suffix: str):
    """Read direct files plus one shard level without exceeding Bridge list caps."""
    base = f"{market}/{folder}"
    try:
        entries = drive.list(base)
    except RuntimeError as exc:
        if "FOLDER_NOT_FOUND" in str(exc):
            return []
        raise
    paths = []
    for entry in entries:
        name = entry["name"]
        if entry.get("mimeType") == "application/vnd.google-apps.folder":
            shard = f"{base}/{name}"
            for child in drive.list(shard):
                if child["name"].endswith(suffix):
                    paths.append(f"{shard}/{child['name']}")
        elif name.endswith(suffix):
            paths.append(f"{base}/{name}")
    return paths


def build(drive: Drive, market: str, date: str):
    state = load_market(drive, market)
    universe = current_universe(drive, market)
    daily = defaultdict(list)
    for day in daily_segments(drive, market):
        for path in read_files(drive, market, "DAILY/" + day, ".ndjson.gz"):
            for row in parse_lines_gz(drive.read(path)):
                if row.get("trade_date", row["date"]) <= date:
                    daily[row["security_id"]].append(row)
    patches = defaultdict(list)
    for path in read_files(drive, market, "REPAIR_PATCH", ".json"):
        payload = drive.json(path)
        # Phase 1 repair evidence may be stored either as one accepted patch
        # per file or as a bulk sidecar containing multiple accepted patches.
        # Bulk storage changes only write granularity; each item keeps the same
        # patch schema/evidence and compose semantics.
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            for patch in items:
                if patch.get("security_id"):
                    patches[patch["security_id"]].append(patch)
        elif isinstance(payload, dict) and payload.get("security_id"):
            patches[payload["security_id"]].append(payload)
    events = []
    for path in read_files(drive, market, "CORPORATE_ACTIONS", ".json"):
        events.extend(drive.json(path))
    anchors_path = f"{market}/SIGNAL_ANCHORS.json"
    anchor_rows = drive.json(anchors_path) if drive.file(anchors_path) else []
    anchors = defaultdict(list)
    for anchor in anchor_rows:
        anchors[anchor["security_id"]].append(anchor)
    derived = []
    def process_base_batch(batch: int) -> list[dict]:
        # Each worker owns an independent HTTP session; Drive access is read-only
        # here. Result writes remain single-threaded below.
        reader = Drive()
        base = parse_lines_gz(reader.read(f"{market}/BASE/batch-{batch:04d}.ndjson.gz"))
        by_id = defaultdict(list)
        for row in base:
            by_id[row["security_id"]].append(row)
        out = []
        start = (batch - 1) * 100
        for security in universe[start:min(start + 100, len(state.securities))]:
            if security.get("listing_status", "ACTIVE") != "ACTIVE":
                continue
            sid = security["security_id"]
            rows = compose(by_id[sid], patches[sid], daily[sid])
            if rows:
                adjusted = split_adjust(rows, events)
                indicator = indicators(adjusted)
                indicator["mae_mfe"] = calculate_anchors(adjusted, anchors[sid])
                out.append(indicator)
        return out

    workers = max(1, min(6, int(os.getenv("DERIVED_READ_WORKERS", "4"))))
    batches = range(1, state.checkpoint["total_batches"] + 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(process_base_batch, batches):
            derived.extend(part)
    for security in universe[len(state.securities):]:
        if security.get("listing_status", "ACTIVE") != "ACTIVE":
            continue
        sid = security["security_id"]
        rows = compose([], patches[sid], daily[sid])
        if rows:
            adjusted = split_adjust(rows, events)
            indicator = indicators(adjusted)
            indicator["mae_mfe"] = calculate_anchors(adjusted, anchors[sid])
            derived.append(indicator)
    # Cross-sectional relative strength uses the current universe itself.
    # It does not create or imply a benchmark before one is specified.
    returns = sorted(row["return_20d"] for row in derived if row["return_20d"] is not None)
    for row in derived:
        if row["return_20d"] is not None and len(returns) > 1:
            import bisect
            row["relative_strength_20d"] = bisect.bisect_right(returns, row["return_20d"]) / len(returns)
        row["filter_pass"] = (row["ma"][20] is not None and
                              row["dollar_volume_20d"] is not None and
                              row["trade_date"] == date)
        row["filter_reason"] = None if row["filter_pass"] else "SHORT_OR_STALE_HISTORY"
        liquidity = row["dollar_volume_20d"] or 0
        row["rank_score"] = (int(row["alignment"] == "BULL") * 2 +
                             int(row["bottom_confirmation"]) +
                             min(liquidity / 1_000_000, 1) +
                             (row["relative_strength_20d"] or 0)) if row["filter_pass"] else None
        row["rank_role"] = "RESEARCH_ONLY"
        row["mae_mfe_reason"] = None if row["mae_mfe"] else "SIGNAL_OR_ENTRY_ANCHOR_REQUIRED"
    derived.sort(key=lambda r: (r["rank_score"] is None,
                                -(r["rank_score"] or 0), r["security_id"]))
    # No benchmark is built or assumed. Relative strength stays null until
    # the user supplies an explicit benchmark list.
    folder = f"{market}/DERIVED/{date}"
    for offset in range(0, len(derived), 250):
        path = f"{folder}/batch-{offset // 250 + 1:04d}.json"
        payload = compact({"market": market, "as_of": date,
                           "rows": derived[offset:offset + 250]})
        if not drive.file(path) or digest(drive.read(path)) != digest(payload):
            drive.put(path, payload)
    path = f"{folder}/RANK.json"
    payload = compact({"market": market, "as_of": date,
                       "rows": [{"security_id": row["security_id"], "rank_score": row["rank_score"],
                                 "filter_pass": row["filter_pass"]} for row in derived],
                       "detail_parts": (len(derived) + 249) // 250,
                       "filters": {"minimum_history_20d": True, "current_date": date},
                       "benchmark": None, "mae_mfe_anchor": None})
    if not drive.file(path) or digest(drive.read(path)) != digest(payload):
        drive.put(path, payload)
    return len(derived)
