"""Recompute market indicators after a completed daily run."""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict

from runner import Drive, compact, digest, load_market, now_myt, parse_lines_gz, map_drive_reads
from foundation import current_universe, daily_segments
from analytics import compose, excursions, indicators, split_adjust
LOG = logging.getLogger("hunter")


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


def read_files(drive: Drive, market: str, folder: str, suffix):
    """Read direct files plus one shard level without exceeding Bridge list caps."""
    suffixes = (suffix,) if isinstance(suffix, str) else tuple(suffix)
    base = f"{market}/{folder}"
    try:
        entries = drive.list(base)
    except RuntimeError as exc:
        if "FOLDER_NOT_FOUND" in str(exc):
            return []
        raise

    paths = []
    shards = []

    for entry in entries:
        name = entry["name"]
        if entry.get("mimeType") == "application/vnd.google-apps.folder":
            shards.append(f"{base}/{name}")
        elif name.endswith(suffixes):
            paths.append(f"{base}/{name}")

    def list_shard(reader, shard):
        return [
            f"{shard}/{child['name']}"
            for child in reader.list(shard)
            if child["name"].endswith(suffixes)
        ]

    workers = max(1, min(6, int(os.getenv("DERIVED_READ_WORKERS", "4"))))
    for result in map_drive_reads(drive, list_shard, shards, workers):
        paths.extend(result)

    return sorted(paths)


def build(drive: Drive, market: str, date: str, daily_rows=None, patch_snapshot=None):
    # Publish only a complete, successful build's inputs to the next stage.
    # Each pending date replaces the snapshot after its daily writes finish.
    if patch_snapshot is not None:
        patch_snapshot.clear()
    from continuation import enabled as continue_only, OutputProgress
    progress = OutputProgress(drive, market, date, 'DERIVED_OUTPUTS') if continue_only() else None
    if progress and progress.doc.get('status') == 'COMPLETE':
        LOG.info('DERIVED_ALREADY_COMMITTED market=%s date=%s', market, date)
        return progress.doc['rows']
    LOG.info("DERIVED_STAGE market=%s date=%s stage=load_inputs", market, date)
    state = load_market(drive, market)
    universe = current_universe(drive, market)
    workers = max(1, min(6, int(os.getenv("DERIVED_READ_WORKERS", "4"))))

    def read_gzip(reader, path):
        return parse_lines_gz(reader.read(path))

    def read_json(reader, path):
        return reader.json(path)

    daily = defaultdict(list)
    daily_paths = []
    if daily_rows is None:
        for day in daily_segments(drive, market):
            daily_paths.extend(
                read_files(drive, market, "DAILY/" + day,
                           (".ndjson.gz", ".ndjson.gzip"))
            )
        segments = map_drive_reads(drive, read_gzip, daily_paths, workers)
    else:
        LOG.info("DERIVED_DAILY_REUSED market=%s date=%s rows=%d", market, date,
                 sum(len(rows) for rows in daily_rows.values()))
        segments = daily_rows.values()
    for rows in segments:
        for row in rows:
            if row.get("trade_date", row["date"]) <= date:
                daily[row["security_id"]].append(row)

    patches = defaultdict(list)
    LOG.info("DERIVED_STAGE market=%s date=%s stage=patch_inventory", market, date)
    patch_paths = read_files(drive, market, "REPAIR_PATCH", ".json")
    LOG.info("DERIVED_STAGE market=%s date=%s stage=patch_reads files=%d", market, date, len(patch_paths))

    for number, payload in enumerate(map_drive_reads(drive, read_json, patch_paths, workers), 1):
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            for patch in items:
                if patch.get("security_id"):
                    patches[patch["security_id"]].append(patch)
        elif isinstance(payload, dict) and payload.get("security_id"):
            patches[payload["security_id"]].append(payload)
        LOG.info("DERIVED_PATCH_PROGRESS market=%s date=%s completed=%d total=%d",
                 market, date, number, len(patch_paths))

    events = []
    action_paths = read_files(drive, market, "CORPORATE_ACTIONS", ".json")

    for payload in map_drive_reads(drive, read_json, action_paths, workers):
        events.extend(payload)
    anchors_path = f"{market}/SIGNAL_ANCHORS.json"
    anchor_rows = drive.json(anchors_path) if drive.file(anchors_path) else []
    anchors = defaultdict(list)
    for anchor in anchor_rows:
        anchors[anchor["security_id"]].append(anchor)
    from derived_resume import BatchResults, context_hash, source_identity, verify_source
    results = BatchResults(drive, market, date, context_hash(
        market, date, universe, len(state.securities), daily, patches, events, anchors))
    derived = []
    def process_base_batch(reader, batch: int):
        # Each worker owns an independent HTTP session; Drive access is read-only
        # here. Result writes remain single-threaded below.
        path = f"{market}/BASE/batch-{batch:04d}.ndjson.gz"
        source = source_identity(reader.file(path))
        restored = results.restore(reader, batch, source)
        if restored is not None:
            return batch, source, restored, True
        raw = reader.read(path)
        verify_source(raw, source)
        base = parse_lines_gz(raw)
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
        return batch, source, out, False

    batches = range(1, state.checkpoint["total_batches"] + 1)
    LOG.info("DERIVED_STAGE market=%s date=%s stage=base_reads batches=%d",
             market, date, state.checkpoint["total_batches"])
    for number, (batch, source, part, reused) in enumerate(
            map_drive_reads(drive, process_base_batch, batches, workers), 1):
        if reused:
            LOG.info('DERIVED_BATCH_REUSED market=%s date=%s batch=%d rows=%d',
                     market, date, batch, len(part))
        else:
            results.save(batch, source, part)
        derived.extend(part)
        LOG.info("DERIVED_BATCH market=%s date=%s completed=%d total=%d",
                 market, date, number, state.checkpoint["total_batches"])
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
    LOG.info("DERIVED_STAGE market=%s date=%s stage=write_results rows=%d", market, date, len(derived))
    for offset in range(0, len(derived), 250):
        path = f"{folder}/batch-{offset // 250 + 1:04d}.json"
        payload = compact({"market": market, "as_of": date,
                           "rows": derived[offset:offset + 250]})
        if progress:
            progress.put(path, payload)
        elif not drive.file(path) or digest(drive.read(path)) != digest(payload):
            drive.put(path, payload)
    path = f"{folder}/RANK.json"
    payload = compact({"market": market, "as_of": date,
                       "rows": [{"security_id": row["security_id"], "rank_score": row["rank_score"],
                                 "filter_pass": row["filter_pass"]} for row in derived],
                       "detail_parts": (len(derived) + 249) // 250,
                       "filters": {"minimum_history_20d": True, "current_date": date},
                       "benchmark": None, "mae_mfe_anchor": None})
    if progress:
        progress.put(path, payload)
        progress.complete(len(derived))
    elif not drive.file(path) or digest(drive.read(path)) != digest(payload):
        drive.put(path, payload)
    if patch_snapshot is not None:
        patch_snapshot.update(market=market, as_of=date, files=len(patch_paths),
                              patches=dict(patches))
    return len(derived)
