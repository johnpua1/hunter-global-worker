"""Idempotent closeout for COMPLETE daily receipts left behind by an interrupted rank/checkpoint stage.

This path never fetches market prices and never rewrites DAILY data. It only:
1) detects the next contiguous COMPLETE receipt after CONTROL/DAILY_CHECKPOINT;
2) reuses an already-valid RANK or rebuilds DERIVED from committed Drive data;
3) CAS-advances the current checkpoint after validation.

It exists so retries do not restart the expensive foundation read/fetch path merely
because a prior execution died after the receipt commit.
"""
from __future__ import annotations

import json

from runner import compact, digest, now_myt


def _validate_receipt(run: dict, market: str, date: str) -> None:
    if (run.get("market") != market or run.get("trade_date") != date
            or run.get("status") != "COMPLETE"
            or type(run.get("active")) is not int or run["active"] < 1
            or type(run.get("available")) is not int
            or not 0 < run["available"] <= run["active"]):
        raise RuntimeError("DAILY_CLOSEOUT_RECEIPT_INVALID")


def _validate_rank(drive, market: str, date: str, run: dict) -> dict:
    rank = drive.json(f"{market}/DERIVED/{date}/RANK.json")
    rows = rank.get("rows")
    parts = rank.get("detail_parts")
    if (rank.get("market") != market or rank.get("as_of") != date
            or not isinstance(rows, list) or not rows
            or len(rows) < run["available"]
            or type(parts) is not int
            or parts != (len(rows) + 249) // 250):
        raise RuntimeError("DAILY_CLOSEOUT_RANK_INVALID")
    for number in range(1, parts + 1):
        if not drive.file(f"{market}/DERIVED/{date}/batch-{number:04d}.json"):
            raise RuntimeError(f"DAILY_CLOSEOUT_DETAIL_MISSING:{number}")
    return rank


def closeout_committed_receipts(drive, market: str) -> list[str]:
    """Close contiguous COMPLETE receipts without re-fetching market data."""
    checkpoint_path = f"{market}/CONTROL/DAILY_CHECKPOINT.json"
    closed: list[str] = []

    while True:
        raw = drive.read(checkpoint_path)
        checkpoint = json.loads(raw)
        last = checkpoint.get("last_completed_date")
        if checkpoint.get("market") != market or not isinstance(last, str):
            raise RuntimeError("DAILY_CLOSEOUT_CHECKPOINT_INVALID")

        from runner import closed_dates_since
        pending = closed_dates_since(market, last)
        if not pending:
            return closed
        date = pending[0]
        receipt_path = f"{market}/CONTROL/DAILY_RUN_{date}.json"
        if not drive.file(receipt_path):
            return closed
        receipt_raw = drive.read(receipt_path)
        run = json.loads(receipt_raw)
        _validate_receipt(run, market, date)

        rank_path = f"{market}/DERIVED/{date}/RANK.json"
        if drive.file(rank_path):
            _validate_rank(drive, market, date, run)
        else:
            from derived import build
            build(drive, market, date)
            _validate_rank(drive, market, date, run)

        if drive.read(receipt_path) != receipt_raw:
            raise RuntimeError("DAILY_CLOSEOUT_RECEIPT_CHANGED")

        live_raw = drive.read(checkpoint_path)
        if live_raw != raw:
            live = json.loads(live_raw)
            if live.get("last_completed_date") == date:
                closed.append(date)
                continue
            raise RuntimeError("DAILY_CLOSEOUT_CONCURRENT_WRITER_CONFLICT")

        updated = dict(checkpoint)
        updated["last_completed_date"] = date
        updated["updated_at_myt"] = now_myt()
        drive.put(checkpoint_path, compact(updated), expected_sha=digest(raw))
        final = drive.json(checkpoint_path)
        if final.get("last_completed_date") != date:
            raise RuntimeError("DAILY_CLOSEOUT_CHECKPOINT_READBACK_FAILED")
        closed.append(date)
