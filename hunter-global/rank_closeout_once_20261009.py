"""One-shot, date-scoped Hunter DAILY rank closeout. Does not touch Phase 2."""
from __future__ import annotations

import argparse
import json
import os

from runner import Drive, closed_dates_since, compact, digest, now_myt
from derived import build
from daily_health import verify_market
from production_guard import check_at_start

TARGET_DATE = "2026-10-08"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", required=True, choices=("US", "HK"))
    parser.add_argument("--date", default=TARGET_DATE)
    args = parser.parse_args()
    market, date = args.market, args.date
    if date != TARGET_DATE:
        raise RuntimeError("RANK_CLOSEOUT_DATE_NOT_AUTHORIZED")
    if os.getenv("HUNTER_ACTIONS_CUTOVER") != "CONFIRMED":
        raise RuntimeError("SINGLE_WRITER_NOT_CONFIRMED")
    check_at_start()
    drive = Drive()
    drive.health()
    receipt_path = f"{market}/CONTROL/DAILY_RUN_{date}.json"
    checkpoint_path = f"{market}/CONTROL/DAILY_CHECKPOINT.json"
    receipt_raw = drive.read(receipt_path)
    run = json.loads(receipt_raw)
    if (run.get("market") != market or run.get("trade_date") != date or
            run.get("status") != "COMPLETE" or
            type(run.get("active")) is not int or run["active"] < 1 or
            type(run.get("available")) is not int or
            not 0 < run["available"] <= run["active"]):
        raise RuntimeError("DAILY_RUN_INVALID_FOR_RANK_CLOSEOUT")

    checkpoint_raw = drive.read(checkpoint_path)
    checkpoint = json.loads(checkpoint_raw)
    last = checkpoint.get("last_completed_date")
    if checkpoint.get("market") != market or not isinstance(last, str):
        raise RuntimeError("DAILY_CHECKPOINT_INVALID_FOR_RANK_CLOSEOUT")
    if last > date:
        raise RuntimeError("DAILY_CHECKPOINT_ALREADY_BEYOND_TARGET")
    if last < date:
        dates = closed_dates_since(market, last)
        if not dates or dates[0] != date:
            raise RuntimeError("NONCONTIGUOUS_DAILY_CHECKPOINT")

    print(f"START market={market} date={date} previous_checkpoint={last} "
          f"active={run['active']} available={run['available']}", flush=True)
    row_count = build(drive, market, date)
    rank_path = f"{market}/DERIVED/{date}/RANK.json"
    rank = drive.json(rank_path)
    rows = rank.get("rows")
    if (rank.get("market") != market or rank.get("as_of") != date or
            not isinstance(rows, list) or not rows or len(rows) != row_count or
            len(rows) < run["available"] or
            type(rank.get("detail_parts")) is not int or
            rank["detail_parts"] != (len(rows) + 249) // 250):
        raise RuntimeError("RANK_SCHEMA_OR_COVERAGE_FAILED")

    universe = drive.json(f"{market}/CURRENT_UNIVERSE.json")
    active_ids = {s["security_id"] for s in universe.get("securities", [])
                  if s.get("listing_status", "ACTIVE") == "ACTIVE"}
    ids = [r.get("security_id") for r in rows]
    if (len(ids) != len(set(ids)) or not set(ids).issubset(active_ids) or
            len(active_ids) != run["active"]):
        raise RuntimeError("RANK_UNIVERSE_IDENTITY_FAILED")
    passed = 0
    for row in rows:
        if type(row.get("filter_pass")) is not bool:
            raise RuntimeError("RANK_FILTER_SCHEMA_INVALID")
        score = row.get("rank_score")
        if row["filter_pass"]:
            if type(score) not in (int, float):
                raise RuntimeError("RANK_FILTERED_SCORE_INVALID")
            passed += 1
        elif score is not None:
            raise RuntimeError("RANK_STALE_SECURITY_SCORED")
    if passed <= 0:
        raise RuntimeError("RANK_NO_CURRENT_SESSION_CANDIDATES")
    for number in range(1, rank["detail_parts"] + 1):
        path = f"{market}/DERIVED/{date}/batch-{number:04d}.json"
        if not drive.file(path):
            raise RuntimeError(f"RANK_DETAIL_PART_MISSING:{number}")

    if drive.read(receipt_path) != receipt_raw:
        raise RuntimeError("DAILY_RUN_CHANGED_DURING_RANK_BUILD")
    live_checkpoint_raw = drive.read(checkpoint_path)
    if live_checkpoint_raw == checkpoint_raw and last < date:
        updated = dict(checkpoint)
        updated["last_completed_date"] = date
        updated["updated_at_myt"] = now_myt()
        drive.put(checkpoint_path, compact(updated), expected_sha=digest(checkpoint_raw))
        print(f"CHECKPOINT_COMMITTED market={market} date={date}", flush=True)
    elif live_checkpoint_raw != checkpoint_raw:
        live_checkpoint = json.loads(live_checkpoint_raw)
        if live_checkpoint.get("last_completed_date") != date:
            raise RuntimeError("CHECKPOINT_CONCURRENT_WRITER_CONFLICT")
        print(f"CHECKPOINT_ALREADY_COMMITTED_OTHER_WRITER market={market} date={date}", flush=True)
    else:
        print(f"CHECKPOINT_ALREADY_CURRENT market={market} date={date}", flush=True)

    final_checkpoint = drive.json(checkpoint_path)
    if final_checkpoint.get("last_completed_date") != date:
        raise RuntimeError("CHECKPOINT_READBACK_FAILED")
    result = verify_market(drive, market)
    if result.get("status") != "HEALTHY" or result.get("last_completed_date") != date:
        raise RuntimeError("DAILY_HEALTH_POSTCHECK_FAILED")
    print(f"FINAL market={market} date={date} status=DAILY_RANK_CLOSED "
          f"rank_rows={len(rows)} current_filter_pass={passed} "
          f"excluded_or_stale={len(rows) - passed} "
          f"daily_run_repairs={run.get('repairs')} "
          "phase2=NOT_TOUCHED queue=NOT_SUPERSEDED", flush=True)


if __name__ == "__main__":
    main()
