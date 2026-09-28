"""One daily Cloud Run maintenance invocation for both Hunter markets.

Repair runs every day. Weekly listing and monthly option refreshes catch up
after a missed scheduled execution. No BASE or VERIFIED path is written.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time

from foundation import current_universe, initialize_control, seed_corporate_actions
from market_calendar import materialize
from options import monthly
from repair import run_repair
from runner import Drive, MARKETS, TZ
from universe import refresh


LOG = logging.getLogger("hunter.maintenance")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from production_guard import check_at_start
    check_at_start()
    if os.getenv("HUNTER_ACTIONS_CUTOVER") != "CONFIRMED":
        raise RuntimeError("SINGLE_WRITER_NOT_CONFIRMED")
    drive = Drive()
    drive.health()
    today = dt.datetime.now(TZ).date()
    deadline = time.monotonic() + 3300
    for market in MARKETS:
        current_universe(drive, market)
        checkpoint = initialize_control(drive, market)
        seed_corporate_actions(drive, market)
        current = drive.json(f"{market}/CURRENT_UNIVERSE.json")
        stamp = dt.datetime.fromisoformat(current["updated_at_myt"]).date()
        if stamp.isocalendar()[:2] != today.isocalendar()[:2]:
            LOG.info("universe market=%s result=%s", market, refresh(drive, market))
        materialize(drive, market, max(checkpoint["last_completed_date"],
                                       dt.date(2026, 9, 25).isoformat()))
        LOG.info("options market=%s rows=%d", market, monthly(drive, market))
    for market in MARKETS:
        if time.monotonic() >= deadline - 120:
            break
        LOG.info("repair market=%s result=%s", market, run_repair(drive, market,
                                                       deadline=deadline))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        from production_guard import safe_error_summary
        LOG.error("HUNTER_EXECUTION_FAILED worker=%s myt=%s reason=%s",
                  os.getenv("CLOUD_RUN_JOB", "local"), dt.datetime.now(TZ).isoformat(timespec="seconds"),
                  safe_error_summary(exc))
        raise
