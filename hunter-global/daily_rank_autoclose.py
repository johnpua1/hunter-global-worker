"""Automatic DAILY ranking closeout from *current-session* verified indicator shards.

Reads only the pending session's COMPLETE receipt, its precomputed dated
DERIVED_BATCHES cache, existing dated output, and the current checkpoint.
Never reads BASE, old DAILY sessions, historical Phase 2, or price feeds.
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
import os
import re

from runner import Drive, closed_dates_since, compact, digest, map_drive_reads, now_myt
from production_guard import check_at_start

LOG = logging.getLogger("hunter.autorank")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_WORKERS = 4


class NotReady(Exception):
    """Current session has not finished materializing; next schedule retries."""


def validate_receipt(run: dict, market: str, date: str) -> None:
    if (run.get("market") != market or run.get("trade_date") != date
            or run.get("status") != "COMPLETE"
            or type(run.get("active")) is not int or run["active"] <= 0
            or type(run.get("available")) is not int
            or not 0 < run["available"] <= run["active"]):
        raise RuntimeError("AUTORANK_COMPLETE_RECEIPT_REQUIRED")


def load_dated_indicators(drive: Drive, market: str, date: str, run: dict) -> list[dict]:
    root = f"{market}/CONTROL/PHASE2_RESUME/DERIVED_BATCHES/{date}"
    try:
        folders = drive.list(root)
    except RuntimeError as exc:
        if "FOLDER_NOT_FOUND" in str(exc):
            raise NotReady("CURRENT_DATE_DERIVED_SNAPSHOT_NOT_READY") from exc
        raise
    expected = math.ceil(run["active"] / 100)
    names = [x["name"] for x in folders
             if x.get("mimeType") == "application/vnd.google-apps.folder"]
    expected_names = [f"batch-{n:04d}" for n in range(1, expected + 1)]
    if sorted(names) != expected_names or len(folders) != expected:
        raise NotReady(f"CURRENT_DATE_BATCHES_INCOMPLETE:{len(names)}/{expected}")

    def read_one(reader: Drive, batch_name: str):
        folder = f"{root}/{batch_name}"
        entries = reader.list(folder)
        if (len(entries) != 1 or
                not re.fullmatch(r"[0-9a-f]{64}\.json", entries[0].get("name", ""))):
            raise NotReady("CURRENT_DATE_VERIFIED_SHARD_INCOMPLETE:" + batch_name)
        path = folder + "/" + entries[0]["name"]
        doc = reader.json(path)
        rows = doc.get("rows")
        if (doc.get("schema") != 1 or
                not isinstance(rows, list) or not 0 < len(rows) <= 100 or
                not HEX64.fullmatch(doc.get("input_sha256", "")) or
                doc.get("rows_sha256") != digest(compact(rows))):
            raise RuntimeError("CURRENT_DATE_VERIFIED_SHARD_INVALID:" + batch_name)
        return rows

    rows: list[dict] = []
    for part in map_drive_reads(drive, read_one, expected_names,
                                min(MAX_WORKERS, max(1, int(os.getenv("DERIVED_READ_WORKERS", "4"))))):
        rows.extend(part)

    ids = [row.get("security_id") for row in rows]
    if (not run["available"] <= len(rows) <= run["active"] or
            len(ids) != len(set(ids)) or
            any(not isinstance(sid, str) or not re.fullmatch(
                market + r"-\d{6}", sid) for sid in ids) or
            any(not isinstance(row, dict) or
                not isinstance(row.get("trade_date"), str) or
                row["trade_date"] > date or
                row.get("rank_score") is not None or
                row.get("relative_strength_20d") is not None
                for row in rows)):
        raise RuntimeError("CURRENT_DATE_INDICATOR_COVERAGE_INVALID")
    return rows


def rank_current(rows: list[dict], date: str) -> list[dict]:
    """Exact existing derived.build score, filter, ordering and record schema."""
    returns = sorted(row["return_20d"] for row in rows if row.get("return_20d") is not None)
    for row in rows:
        if row.get("return_20d") is not None and len(returns) > 1:
            row["relative_strength_20d"] = (
                bisect.bisect_right(returns, row["return_20d"]) / len(returns))
        row["filter_pass"] = (
            isinstance(row.get("ma"), dict) and row["ma"].get("20") is not None
            and row.get("dollar_volume_20d") is not None and
            row.get("trade_date") == date)
        row["filter_reason"] = None if row["filter_pass"] else "SHORT_OR_STALE_HISTORY"
        liquidity = row.get("dollar_volume_20d") or 0
        row["rank_score"] = (
            int(row["alignment"] == "BULL") * 2
            + int(row["bottom_confirmation"])
            + min(liquidity / 1_000_000, 1)
            + (row.get("relative_strength_20d") or 0)
        ) if row["filter_pass"] else None
        row["rank_role"] = "RESEARCH_ONLY"
        row["mae_mfe_reason"] = (
            None if row.get("mae_mfe") else "SIGNAL_OR_ENTRY_ANCHOR_REQUIRED")
    rows.sort(key=lambda r: (
        r["rank_score"] is None, -(r["rank_score"] or 0), r["security_id"]))
    return rows


def outputs(market: str, date: str, rows: list[dict]) -> list[tuple[str, bytes]]:
    root = f"{market}/DERIVED/{date}"
    artifacts = []
    for offset in range(0, len(rows), 250):
        name = f"batch-{offset // 250 + 1:04d}.json"
        artifacts.append((f"{root}/{name}",
                          compact({"market": market, "as_of": date,
                                   "rows": rows[offset:offset + 250]})))
    rank = {"market": market, "as_of": date,
            "rows": [{"security_id": r["security_id"], "rank_score": r["rank_score"],
                      "filter_pass": r["filter_pass"]} for r in rows],
            "detail_parts": len(artifacts),
            "filters": {"minimum_history_20d": True, "current_date": date},
            "benchmark": None, "mae_mfe_anchor": None}
    artifacts.append((f"{root}/RANK.json", compact(rank)))
    return artifacts


def close_one(drive: Drive, market: str) -> str:
    checkpoint_path = f"{market}/CONTROL/DAILY_CHECKPOINT.json"
    raw_cp = drive.read(checkpoint_path)
    cp = json.loads(raw_cp)
    last = cp.get("last_completed_date")
    if cp.get("market") != market or not isinstance(last, str):
        raise RuntimeError("AUTORANK_CHECKPOINT_INVALID")
    pending = closed_dates_since(market, last)
    if not pending:
        return "UP_TO_DATE"
    date = pending[0]
    receipt_path = f"{market}/CONTROL/DAILY_RUN_{date}.json"
    if not drive.file(receipt_path):
        return "COMPLETE_RECEIPT_NOT_READY:" + date
    raw_run = drive.read(receipt_path)
    run = json.loads(raw_run)
    validate_receipt(run, market, date)

    rank_path = f"{market}/DERIVED/{date}/RANK.json"
    if drive.file(rank_path):
        from daily_closeout import _validate_rank
        existing = _validate_rank(drive, market, date, run)
        if len(existing["rows"]) > run["active"]:
            raise RuntimeError("AUTORANK_EXISTING_COVERAGE_INVALID")
        ranked = len(existing["rows"])
        passed = sum(r["filter_pass"] for r in existing["rows"])
    else:
        rows = rank_current(load_dated_indicators(drive, market, date, run), date)
        artifacts = outputs(market, date, rows)
        # Preflight *all* partial output conflicts before any new writes.
        missing = []
        for path, payload in artifacts:
            if drive.file(path):
                if drive.read(path) != payload:
                    raise RuntimeError("AUTORANK_EXISTING_DATED_OUTPUT_CONFLICT:" + path)
            else:
                missing.append((path, payload))
        if drive.read(receipt_path) != raw_run:
            raise RuntimeError("AUTORANK_RECEIPT_CHANGED_DURING_BUILD")
        if drive.read(checkpoint_path) != raw_cp:
            raise NotReady("AUTORANK_CHECKPOINT_CONCURRENT_CHANGE")
        # RANK is committed last, and only after every detail part is durable.
        for path, payload in missing:
            drive.append(path, payload)
            if drive.read(path) != payload:
                raise RuntimeError("AUTORANK_DATED_WRITE_READBACK_FAILED:" + path)
        from daily_closeout import _validate_rank
        _validate_rank(drive, market, date, run)
        ranked = len(rows)
        passed = sum(r["filter_pass"] for r in rows)

    if drive.read(receipt_path) != raw_run:
        raise RuntimeError("AUTORANK_COMPLETE_RECEIPT_CHANGED")
    current = drive.read(checkpoint_path)
    if current == raw_cp:
        updated = dict(cp, last_completed_date=date, updated_at_myt=now_myt())
        drive.put(checkpoint_path, compact(updated), expected_sha=digest(raw_cp))
    else:
        current_doc = json.loads(current)
        if current_doc.get("last_completed_date") != date:
            raise NotReady("AUTORANK_CONCURRENT_WRITER_ADVANCED")
    final = drive.json(checkpoint_path)
    if final.get("last_completed_date") != date:
        raise RuntimeError("AUTORANK_CHECKPOINT_READBACK_FAILED")
    return f"CLOSED market={market} date={date} ranked={ranked} filter_pass={passed}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", required=True, choices=("US", "HK"))
    args = parser.parse_args()
    check_at_start()
    if os.environ.get("HUNTER_ACTIONS_CUTOVER") != "CONFIRMED":
        raise RuntimeError("SINGLE_WRITER_NOT_CONFIRMED")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    drive = Drive()
    try:
        drive.health()
        try:
            print("AUTORANK " + close_one(drive, args.market), flush=True)
        except NotReady as exc:
            print("AUTORANK RETRY_NEXT_SCHEDULE:" + str(exc), flush=True)
    finally:
        drive.http.close()


if __name__ == "__main__":
    main()
