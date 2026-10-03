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


def repair_markets(drive, markets, deadline):
    """Share remaining wall time; a first-market failure must not skip the second."""
    failures = []
    for index, market in enumerate(markets):
        now = time.monotonic()
        share = (deadline - now) / (len(markets) - index)
        if share <= 300:
            LOG.error("maintenance stage=repair market=%s state=SKIPPED reason=TIME_BUDGET", market)
            failures.append(market)
            continue
        LOG.info("maintenance stage=repair market=%s state=START budget_seconds=%d", market, share)
        try:
            result = run_repair(drive, market, deadline=now + share)
            LOG.info("repair market=%s result=%s", market, result)
            if result.get("retry_exhausted"):
                LOG.error("HUNTER_REPAIR_REVIEW_REQUIRED market=%s count=%s",
                          market, result["retry_exhausted"])
            if result.get("timed_out"):
                failures.append(market)
        except Exception as exc:
            from production_guard import safe_error_summary
            LOG.error("maintenance stage=repair market=%s state=FAILED reason=%s",
                      market, safe_error_summary(exc))
            failures.append(market)
    if failures:
        # Cloud Run's bounded retry resumes persisted queue state automatically.
        raise RuntimeError("MAINTENANCE_REPAIR_INCOMPLETE:" + ",".join(failures))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    job = os.getenv("CLOUD_RUN_JOB")
    if job and job != "hunter-maintenance":
        raise RuntimeError(f"TOPOLOGY_RUNTIME_MISMATCH:{job}:expected=hunter-maintenance")
    from production_guard import check_at_start
    check_at_start()
    lock_probe = os.getenv("HUNTER_MAINTENANCE_LOCK_PROBE", "")
    if lock_probe not in ("", "VERIFY"):
        raise RuntimeError("MAINTENANCE_LOCK_PROBE_INVALID")
    if os.getenv("HUNTER_ACTIONS_CUTOVER") != "CONFIRMED":
        raise RuntimeError("SINGLE_WRITER_NOT_CONFIRMED")
    drive = Drive()
    drive.health()
    if lock_probe == "VERIFY":
        from maintenance_lock import verify_lock
        verify_lock(drive)
        return
    from maintenance_lock import maintenance_slot
    with maintenance_slot(drive) as held:
        if held:
            run_maintenance(drive)


def run_maintenance(drive) -> None:
    today = dt.datetime.now(TZ).date()
    deadline = time.monotonic() + 3300
    for market in MARKETS:
        LOG.info("maintenance stage=control market=%s state=START", market)
        current_universe(drive, market)
        checkpoint = initialize_control(drive, market)
        seed_corporate_actions(drive, market)
        current = drive.json(f"{market}/CURRENT_UNIVERSE.json")
        stamp = dt.datetime.fromisoformat(current["updated_at_myt"]).date()
        if stamp.isocalendar()[:2] != today.isocalendar()[:2]:
            LOG.info("universe market=%s result=%s", market, refresh(drive, market))
        LOG.info("maintenance stage=calendar market=%s state=START", market)
        calendar_rows = materialize(drive, market, max(checkpoint["last_completed_date"],
                                       dt.date(2026, 9, 25).isoformat()))
        LOG.info("maintenance stage=calendar market=%s state=DONE rows=%d", market, calendar_rows)
        LOG.info("maintenance stage=options market=%s state=START", market)
        LOG.info("options market=%s rows=%d", market, monthly(drive, market))
    markets = list(MARKETS)
    if today.toordinal() % 2:
        markets.reverse()
    repair_markets(drive, markets, deadline)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        from production_guard import safe_error_summary
        LOG.error("HUNTER_EXECUTION_FAILED worker=%s myt=%s reason=%s",
                  os.getenv("CLOUD_RUN_JOB", "local"), dt.datetime.now(TZ).isoformat(timespec="seconds"),
                  safe_error_summary(exc))
        raise
