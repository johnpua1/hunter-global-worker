"""Persist observed sessions; never infer an exchange halt from missing bars."""
from __future__ import annotations

import datetime as dt

from runner import Drive, compact, closed_dates_since, load_market, now_myt


def materialize(drive: Drive, market: str, as_of: str) -> int:
    state = load_market(drive, market)
    if as_of < state.calendar[-1]:
        raise ValueError("CALENDAR_BEFORE_BASE")
    observed = set(state.calendar)
    if as_of > state.calendar[-1]:
        observed.update(d for d in closed_dates_since(market, state.calendar[-1]) if d <= as_of)
    first = dt.date.fromisoformat(state.calendar[0])
    last = dt.date.fromisoformat(as_of)
    overrides_path = f"{market}/MARKET_CALENDAR/OFFICIAL_OVERRIDES.json"
    overrides = (drive.json(overrides_path) if drive.file(overrides_path) else {})
    zone = "America/New_York" if market == "US" else "Asia/Hong_Kong"
    rows = []
    unresolved = []
    # Index receipts once instead of making a Bridge request for every date
    # in the historical calendar. A capped listing is not proof of absence.
    try:
        control_files = drive.list(f"{market}/CONTROL")
        receipt_names = ({item["name"] for item in control_files}
                         if len(control_files) < 1000 else None)
    except RuntimeError as exc:
        if "LIST_LIMIT" in str(exc):
            receipt_names = None
        elif "FOLDER_NOT_FOUND" in str(exc):
            receipt_names = set()
        else:
            raise
    day = first
    while day <= last:
        date = day.isoformat()
        proof = overrides.get(date, {})
        if proof and not (proof.get("official_source") and proof.get("evidence_ref")):
            raise RuntimeError("CALENDAR_OVERRIDE_MISSING_OFFICIAL_EVIDENCE:" + date)
        status = proof.get("session_status")
        if status not in (None, "OPEN", "CLOSED", "HALF_DAY", "MARKET_HALTED"):
            raise ValueError("CALENDAR_BAD_OFFICIAL_STATUS:" + date)
        outage = f"{market}/CONTROL/DAILY_RUN_{date}.json"
        has_receipt = (drive.file(outage) if receipt_names is None else
                       f"DAILY_RUN_{date}.json" in receipt_names)
        if has_receipt and drive.json(outage).get("status") == "MARKET_WIDE_DATA_UNAVAILABLE":
            status = "MARKET_WIDE_DATA_UNAVAILABLE"
        if status is None:
            if date in observed:
                status = "OPEN"
            elif day.weekday() >= 5:
                status = "CLOSED"
            else:
                # An index bar missing on a weekday is not official closure proof.
                unresolved.append(date)
                day += dt.timedelta(days=1)
                continue
        rows.append({"trade_date": date, "session_status": status,
                     "open_time": proof.get("open_time", "09:30" if status in ("OPEN", "HALF_DAY") else None),
                     "close_time": proof.get("close_time", "16:00" if status == "OPEN" else None),
                     "time_zone": zone, "half_day": status == "HALF_DAY",
                     "source": proof.get("official_source", "BASE_CALENDAR/^GSPC" if market == "US"
                                          else "BASE_CALENDAR/^HSI") if date in observed or proof else
                     "WEEKEND" if day.weekday() >= 5 else "PENDING_OFFICIAL_EVIDENCE",
                     "evidence_ref": proof.get("evidence_ref"), "updated_at_myt": now_myt()})
        day += dt.timedelta(days=1)
    path = f"{market}/MARKET_CALENDAR/as_of_{as_of}.json"
    if not drive.file(path):
        drive.put(path, compact({"market": market, "as_of": as_of,
                                 "sessions": rows, "unverified_weekdays": unresolved}),
                  immutable=True)
    return len(rows)
