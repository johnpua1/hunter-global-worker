"""One daily Cloud Run maintenance invocation for both Hunter markets.

Repair runs every day. Weekly listing and monthly option refreshes catch up
after a missed scheduled execution. No BASE or VERIFIED path is written.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

from foundation import current_universe, seed_corporate_actions
from options import monthly
from repair import run_repair
from runner import Drive, MARKETS, TZ
from universe import refresh


LOG = logging.getLogger("hunter.maintenance")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.getenv("HUNTER_ACTIONS_CUTOVER") != "CONFIRMED":
        raise RuntimeError("SINGLE_WRITER_NOT_CONFIRMED")
    drive = Drive()
    drive.health()
    today = dt.datetime.now(TZ).date()
    for market in MARKETS:
        current_universe(drive, market)
        LOG.info("repair market=%s result=%s", market, run_repair(drive, market))
        seed_corporate_actions(drive, market)
        current = drive.json(f"{market}/CURRENT_UNIVERSE.json")
        stamp = dt.datetime.fromisoformat(current["updated_at_myt"]).date()
        if stamp.isocalendar()[:2] != today.isocalendar()[:2]:
            LOG.info("universe market=%s result=%s", market, refresh(drive, market))
        LOG.info("options market=%s rows=%d", market, monthly(drive, market))


if __name__ == "__main__":
    main()
