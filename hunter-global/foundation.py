"""Append-only Hunter data foundation. BASE and VERIFIED are never written here."""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from runner import (Drive, MARKETS, compact, digest, fetch_security, lines_gz,
                    load_market, now_myt, parse_lines_gz, closed_dates_since)


def current_universe(drive: Drive, market: str) -> list[dict]:
    path = f"{market}/CURRENT_UNIVERSE.json"
    if drive.file(path):
        doc = drive.json(path)
        if doc.get("market") != market or not isinstance(doc.get("securities"), list):
            raise RuntimeError("CURRENT_UNIVERSE_INVALID:" + market)
        securities = doc["securities"]
    else:
        # One-time copy of the existing frozen identity list. No new IDs,
        # source refresh, or BASE reprocessing is performed here.
        base = load_market(drive, market)
        securities = [{**s, "listing_status": "ACTIVE"} for s in base.securities]
        drive.put(path, compact({"market": market, "as_of": base.checkpoint["as_of"],
                                 "securities": securities, "updated_at_myt": now_myt()}),
                  immutable=True)
    ids = [s["security_id"] for s in securities]
    if len(ids) != len(set(ids)) or any(s["market"] != market for s in securities):
        raise RuntimeError("CURRENT_UNIVERSE_IDENTITY_CONFLICT:" + market)
    return securities


def daily_segments(drive: Drive, market: str) -> list[str]:
    folders = drive.list(market)
    if not any(x["name"] == "DAILY" for x in folders):
        return []
    return sorted(x["name"] for x in drive.list(f"{market}/DAILY")
                  if x.get("mimeType") == "application/vnd.google-apps.folder"
                  and len(x["name"]) == 10 and x["name"][4] == "-")


def read_existing(drive: Drive, market: str, securities: list[dict], base_as_of: str):
    """Return the latest stored bar and keys, including older DAILY layouts."""
    last = {s["security_id"]: (base_as_of if s.get("security_id_origin") != "NEW_LISTING"
                                  else None) for s in securities}
    keys = set()
    dates = daily_segments(drive, market)
    for date in dates:
        for file in drive.list(f"{market}/DAILY/{date}"):
            if not file["name"].endswith(".ndjson.gz"):
                continue
            for row in parse_lines_gz(drive.read(f"{market}/DAILY/{date}/{file['name']}")):
                key = (row["security_id"], row.get("trade_date", row.get("date")))
                if key in keys:
                    raise RuntimeError("DAILY_DUPLICATE_STORED:" + str(key))
                keys.add(key)
                if key[0] in last and (last[key[0]] is None or key[1] > last[key[0]]):
                    last[key[0]] = key[1]
    return last, keys


def append_queue(drive: Drive, items: list[dict]):
    if not items:
        return 0
    path = "REPAIR_QUEUE.json"
    for _ in range(5):
        raw = drive.read(path)
        doc = json.loads(raw)
        existing = {(x.get("market"), x.get("security_id"), x.get("category"),
                     x.get("trade_date")) for x in doc["items"]}
        new = [x for x in items if (x["market"], x["security_id"], x["category"],
                                     x.get("trade_date")) not in existing]
        if not new:
            return 0
        doc["items"].extend(new)
        try:
            drive.put(path, compact(doc), expected_sha=digest(raw))
            return len(new)
        except RuntimeError as exc:
            if "BRIDGE_STALE_WRITE" not in str(exc):
                raise
    raise RuntimeError("REPAIR_QUEUE_CAS_EXHAUSTED")


def flag_five_day_failures(drive: Drive, market: str, date: str,
                           securities: list[dict], calendar: list[str]):
    valid_days = sorted(d for d in calendar if d <= date)[-5:]
    if len(valid_days) < 5:
        return 0
    queue = drive.json("REPAIR_QUEUE.json")["items"]
    failures = defaultdict(set)
    for item in queue:
        if (item.get("market") == market and item.get("category") == "FETCH_FAILED" and
                item.get("status", "OPEN") == "OPEN"):
            failures[item.get("security_id")].add(item.get("trade_date"))
    suspects = {sid for sid, days in failures.items() if set(valid_days) <= days}
    if not suspects:
        return 0
    path = f"{market}/CURRENT_UNIVERSE.json"
    raw = drive.read(path)
    doc = json.loads(raw)
    changed = 0
    for security in doc["securities"]:
        if security["security_id"] in suspects and security.get("review_status") != "SUSPECT_DELISTED":
            security["review_status"] = "SUSPECT_DELISTED"
            changed += 1
    if changed:
        drive.put(path, compact(doc), expected_sha=digest(raw))
        append_queue(drive, [{"market": market, "security_id": sid,
                              "category": "SUSPECT_DELISTED", "trade_date": date,
                              "problem": "FIVE_CONSECUTIVE_VALID_SESSION_FAILURES",
                              "status": "OPEN", "recorded_at_myt": now_myt()}
                             for sid in suspects])
    return changed


def append_daily_date(drive: Drive, market: str, date: str, securities: list[dict],
                      last: dict, keys: set, workers: int, base_calendar: list[str]) -> dict:
    active = [s for s in securities if s.get("listing_status", "ACTIVE") == "ACTIVE"]
    # Each security starts the day following its own last stored bar. This
    # recovers multi-session gaps without rereading or rewriting BASE.
    target = [s for s in active if last[s["security_id"]] is None or
              last[s["security_id"]] < date]
    from concurrent.futures import ThreadPoolExecutor
    def history_start(security):
        return (last[security["security_id"]] or
                (security.get("listing_date") if security.get("listing_date_verified") else None) or
                "1970-01-01")
    calendar = closed_dates_since(market, min(map(history_start, target))) if target else []
    calendar = sorted(set(base_calendar + calendar))
    if date not in calendar:
        calendar.append(date)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: fetch_security(
            s, [d for d in calendar if (last[s["security_id"]] or "0000") < d <= date],
            date, daily=True), target))
    available_today = sum((s["security_id"], date) in keys for s in active)
    available_today += sum(any(r["date"] == date for r in result[0])
                           for result in results)
    if active and date in calendar and (len(active) - available_today) / len(active) >= .80:
        # No individual repair flood during source-wide outages. A confirmed
        # exchange halt is a separate, evidence-backed status.
        status = "MARKET_WIDE_DATA_UNAVAILABLE"
        run = {"market": market, "trade_date": date, "status": status,
               "active": len(active), "available": available_today,
               "updated_at_myt": now_myt()}
        drive.put(f"{market}/CONTROL/DAILY_RUN_{date}.json", compact(run))
        return {**run, "written": 0}
    rows, repairs, events = [], [], []
    for security, (found, flags, splits, reason) in zip(target, results):
        sid = security["security_id"]
        events.extend(splits)
        for row in found:
            key = (sid, row["date"])
            if key not in keys:
                rows.append({**row, "trade_date": row["date"]})
                keys.add(key)
                last[sid] = row["date"]
        for flag in ("FETCH_FAILED", "DATA_SUSPECT", "IDENTITY_REVIEW"):
            if flag in flags:
                repairs.append({"market": market, "security_id": sid, "category": flag,
                                "trade_date": date, "problem": reason or flag,
                                "status": "OPEN", "recorded_at_myt": now_myt()})
    rows.sort(key=lambda r: (r["security_id"], r["date"]))
    append_queue(drive, repairs)
    flag_five_day_failures(drive, market, date, securities, calendar)
    if rows:
        folder = f"{market}/DAILY/{date}"
        try:
            names = {x["name"] for x in drive.list(folder)}
        except RuntimeError as exc:
            if "FOLDER_NOT_FOUND" not in str(exc):
                raise
            names = set()
        number = 1
        while f"part-{number:04d}.ndjson.gz" in names:
            number += 1
        path = f"{folder}/part-{number:04d}.ndjson.gz"
        # Only create a new segment. A restarted run checks the actual rows
        # before the call and cannot replace an existing segment.
        drive.append(path, lines_gz(rows), "application/x-gzip")
    if events:
        unique = { (e["security_id"], e["effective_date"], e["factor"]): e
                   for e in events }
        payload = compact(list(unique.values()))
        action_path = f"{market}/CORPORATE_ACTIONS/{date}-{digest(payload)[:16]}.json"
        if not drive.file(action_path):
            drive.append(action_path, payload)
    run = {"market": market, "trade_date": date, "status": "COMPLETE",
           "active": len(active), "available": available_today,
           "written": len(rows), "repairs": len(repairs), "updated_at_myt": now_myt()}
    drive.put(f"{market}/CONTROL/DAILY_RUN_{date}.json", compact(run))
    return run


def run_daily(drive: Drive, market: str, workers: int):
    base = load_market(drive, market)
    if len(base.checkpoint.get("verified_batches", {})) != base.checkpoint["total_batches"]:
        raise RuntimeError("BASE_NOT_VERIFIED:" + market)
    securities = current_universe(drive, market)
    last, keys = read_existing(drive, market, securities, base.checkpoint["as_of"])
    checkpoint = f"{market}/CONTROL/DAILY_CHECKPOINT.json"
    legacy = f"{market}/DAILY/CHECKPOINT.json"
    previous = (drive.json(checkpoint) if drive.file(checkpoint) else
                drive.json(legacy) if drive.file(legacy) else
                {"market": market, "last_completed_date": base.checkpoint["as_of"]})
    results = []
    dates = closed_dates_since(market, previous["last_completed_date"])
    if not dates and any(value is None for value in last.values()):
        dates = [previous["last_completed_date"]]
    for date in dates:
        result = append_daily_date(drive, market, date, securities, last, keys, workers,
                                   base.calendar)
        results.append(result)
        if result["status"] != "COMPLETE":
            break
        previous["last_completed_date"] = date
        previous["updated_at_myt"] = now_myt()
        drive.put(checkpoint, compact(previous))
        update_new_listing_history(drive, market, keys)
        from derived import build
        build(drive, market, date)
    return results


def update_new_listing_history(drive: Drive, market: str, keys: set):
    path = f"{market}/CURRENT_UNIVERSE.json"
    raw = drive.read(path)
    doc = json.loads(raw)
    counts = defaultdict(int)
    for sid, _ in keys:
        counts[sid] += 1
    changed = False
    for security in doc["securities"]:
        if security.get("security_id_origin") == "NEW_LISTING":
            status = "SHORT_HISTORY" if counts[security["security_id"]] < 501 else "FULL_HISTORY"
            if security.get("history_status") != status:
                security["history_status"] = status
                changed = True
    if changed:
        drive.put(path, compact(doc), expected_sha=digest(raw))


def seed_corporate_actions(drive: Drive, market: str):
    """Aggregate existing split_events manifests without touching BASE."""
    state = load_market(drive, market)
    path = f"{market}/CORPORATE_ACTIONS/BASE_SPLITS.json"
    if drive.file(path):
        return 0
    events = {}
    for batch in range(1, state.checkpoint["total_batches"] + 1):
        manifest = drive.json(f"{market}/BASE/batch-{batch:04d}.manifest.json")
        for event in manifest.get("split_events", []):
            key = (event["security_id"], event["effective_date"], event["factor"])
            events[key] = event
    drive.append(path, compact(list(events.values())))
    return len(events)
