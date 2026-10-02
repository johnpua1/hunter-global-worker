"""Fail-closed health gate for Hunter DAILY.

This checker is read-only. It verifies that no completed market session is
missing from CONTROL/DAILY_CHECKPOINT, and that the checkpointed session has
both a COMPLETE run receipt and a DERIVED rank artifact.
"""
from __future__ import annotations

import argparse
import json
import sys

from runner import Drive, closed_dates_since, load_market


def verify_market(drive: Drive, market: str) -> dict:
    checkpoint_path = f"{market}/CONTROL/DAILY_CHECKPOINT.json"
    if not drive.file(checkpoint_path):
        raise RuntimeError(f"DAILY_CHECKPOINT_MISSING:{market}")

    checkpoint = drive.json(checkpoint_path)
    if checkpoint.get("market") != market or not checkpoint.get("last_completed_date"):
        raise RuntimeError(f"DAILY_CHECKPOINT_INVALID:{market}")

    last_completed = checkpoint["last_completed_date"]
    pending = closed_dates_since(market, last_completed)
    if pending:
        raise RuntimeError(
            f"DAILY_STALE:{market}:checkpoint={last_completed}:expected={pending[-1]}"
        )

    base = load_market(drive, market)
    if last_completed < base.checkpoint["as_of"]:
        raise RuntimeError(
            f"DAILY_CHECKPOINT_BEFORE_BASE:{market}:{last_completed}"
        )

    # A checkpoint after BASE must be backed by a successful run receipt.
    if last_completed > base.checkpoint["as_of"]:
        run_path = f"{market}/CONTROL/DAILY_RUN_{last_completed}.json"
        if not drive.file(run_path):
            raise RuntimeError(
                f"DAILY_RUN_RECEIPT_MISSING:{market}:{last_completed}"
            )
        run = drive.json(run_path)
        if (
            run.get("market") != market
            or run.get("trade_date") != last_completed
            or run.get("status") != "COMPLETE"
        ):
            raise RuntimeError(
                f"DAILY_RUN_NOT_COMPLETE:{market}:{last_completed}:"
                + json.dumps(run, ensure_ascii=False, separators=(",", ":"))
            )

    rank_path = f"{market}/DERIVED/{last_completed}/RANK.json"
    if not drive.file(rank_path):
        raise RuntimeError(
            f"DERIVED_RANK_MISSING:{market}:{last_completed}"
        )

    return {
        "market": market,
        "status": "HEALTHY",
        "last_completed_date": last_completed,
        "checkpoint_updated_at_myt": checkpoint.get("updated_at_myt"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=("US", "HK"), required=True)
    args = parser.parse_args()

    drive = Drive()
    drive.health()
    result = verify_market(drive, args.market)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"HUNTER_DAILY_HEALTH_FAILED:{type(exc).__name__}:{exc}", file=sys.stderr)
        sys.exit(2)
