"""Read-only authoritative Phase 2 status projection.

This module distinguishes three independent facts:
- Phase 2 initial baseline acceptance (PASS; governed by user-approved record).
- Production AUTO activation (last confirmed ACTIVE; not a success receipt).
- Phase 2 DAILY freshness (read only from the market's CURRENT checkpoint).

Never scan, download, recalculate, mutate, or revalidate historical market data
merely to answer a Phase 2 status question.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

AUTHORITY_FILE = (Path(__file__).resolve().parent.parent /
                  "docs/hunter-phase2-production-pass-20261009.json")


def interpret_checkpoint(market: str, checkpoint: dict) -> dict:
    """Interpret one market's current daily/Phase2 dates without changing baseline PASS."""
    if market not in ("US", "HK") or not isinstance(checkpoint, dict):
        raise ValueError("PHASE2_CURRENT_CHECKPOINT_INVALID")
    if checkpoint.get("market") != market:
        raise ValueError("PHASE2_CURRENT_CHECKPOINT_MARKET_MISMATCH")
    daily = checkpoint.get("last_completed_date")
    if not isinstance(daily, str) or not daily:
        raise ValueError("PHASE2_CURRENT_DAILY_DATE_MISSING")
    dt.date.fromisoformat(daily)
    phase2 = checkpoint.get("phase2_completed_date")
    if phase2 is None or phase2 == "":
        phase2_date = None
        freshness = "NOT_RECORDED"
    else:
        if not isinstance(phase2, str):
            raise ValueError("PHASE2_COMPLETED_DATE_INVALID")
        dt.date.fromisoformat(phase2)
        phase2_date = phase2
        freshness = ("CURRENT" if phase2 == daily else
                     "BEHIND" if phase2 < daily else "RECEIPT_CONFLICT")
    return {
        "market": market,
        "daily_last_completed_date": daily,
        "phase2_last_completed_date": phase2_date,
        "phase2_daily_freshness": freshness,
    }


def project(drive, markets=("US", "HK"), authority_file=AUTHORITY_FILE) -> dict:
    """Read only current US/HK checkpoint JSON; the acceptance record is local metadata."""
    authority = json.loads(Path(authority_file).read_text(encoding="utf-8"))
    if (authority.get("schema") != "HUNTER_PHASE2_STATUS_AUTHORITY_V2"
            or authority["historical_acceptance"]["status"] != "PASS"
            or authority["production_activation"]["status"] != "ACTIVE"):
        raise ValueError("PHASE2_ACCEPTANCE_AUTHORITY_INVALID")
    return {
        "schema": "HUNTER_PHASE2_STATUS_PROJECTION_V1",
        "historical_acceptance": {
            "status": "PASS",
            "scope": "INITIAL_BASELINE_ONLY",
        },
        "production_activation": {
            "status": "ACTIVE",
            "scope": "LAST_CONFIRMED_AUTO_CONFIGURATION_ONLY",
            "last_verified_at_myt": authority["production_activation"]["last_verified_at_myt"],
        },
        "markets": {
            market: interpret_checkpoint(
                market, drive.json(f"{market}/CONTROL/DAILY_CHECKPOINT.json")
            ) for market in markets
        },
        "invariant": ("Daily rank completion and Phase2 daily freshness are separate "
                      "from the accepted initial Phase2 baseline; do not infer one from another."),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read current Hunter Phase2 status without historical reads")
    parser.add_argument("--market", choices=("US", "HK", "BOTH"), default="BOTH")
    args = parser.parse_args()
    from runner import Drive
    chosen = ("US", "HK") if args.market == "BOTH" else (args.market,)
    print(json.dumps(project(Drive(), chosen), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
