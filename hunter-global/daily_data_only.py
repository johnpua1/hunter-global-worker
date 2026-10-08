"""Independent US/HK daily bars. No ranking, Phase 2 or archive reads.

Only current symbol metadata and bounded operational receipts are read.
Acknowledged dates/batches are skipped. A pending fetch stage is durable before
append, so a lost append response is recovered from metadata, not a data read.
Legacy partial dates without a usable receipt stop rather than guessing keys.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from daily_transport import Drive, compact, digest, lines_gz, now_myt, retry_http
from execution_budget import start, saving, timeout

LOG = logging.getLogger("daily_data_only")
ZONES = {"US": "America/New_York", "HK": "Asia/Hong_Kong"}
FINISHED = {"COMPLETE", "COMPLETE_WITH_GAPS"}


class DailyDrive(Drive):
    """Enforce isolation at the Bridge transport boundary, not just call sites."""
    def __init__(self, market):
        self.market, self.target = market, None
        super().__init__()

    def allowed(self, op, path):
        m = self.market
        own = path in {f"{m}/CONTROL/DAILY_DATA_CHECKPOINT.json", f"{m}/CONTROL/DAILY_DATA_LEASE.json"}
        metadata = path in {f"{m}/CURRENT_UNIVERSE.json", f"{m}/CONTROL/DAILY_CHECKPOINT.json"}
        day = self.target
        state = bool(day and (path == f"{m}/CONTROL/DAILY_RUN_{day}.json"
                    or re.fullmatch(re.escape(f"{m}/CONTROL/DAILY_DATA/{day}/")
                                    + r"(STATE|stage-[0-9]{6})\.json", path)))
        daily = bool(day and re.fullmatch(re.escape(f"{m}/DAILY/{day}/")
                                         + r"part-data-[0-9]{6}\.ndjson\.gz", path))
        if op in {"read", "read_chunk", "read_verified_chunk", "file"}:
            return own or metadata or state or (op == "file" and daily)
        if op == "list":
            return bool(day and path == f"{m}/DAILY/{day}")
        if op == "put":
            return own or state
        if op == "append":
            return daily
        return False

    def _call(self, op, **fields):
        path = fields.get("path", "")
        if not self.allowed(op, path):
            raise RuntimeError("DAILY_DATA_SCOPE_DENIED:" + op + ":" + path)
        return super()._call(op, **fields)


class Record:
    def __init__(self, drive, path):
        self.drive, self.path = drive, path
        self.raw = drive.read(path) if drive.file(path) else None
        self.doc = json.loads(self.raw) if self.raw is not None else None

    def save(self, doc):
        raw = compact(doc)
        with saving():
            self.drive.put(self.path, raw, **({"expected_sha": digest(self.raw)}
                          if self.raw is not None else {"immutable": True}))
        self.doc, self.raw = doc, raw


def chart(symbol, market, first, last):
    """Exact requested date range; no old five-day padding or split history."""
    zone = ZoneInfo(ZONES[market])
    first_date, last_date = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    if first_date > last_date:
        raise ValueError("INVALID_DATE_RANGE")
    begin = dt.datetime.combine(first_date, dt.time(), zone)
    end = dt.datetime.combine(last_date + dt.timedelta(days=1), dt.time(), zone)
    with requests.Session() as session:
        doc = retry_http(session, "GET", "https://query1.finance.yahoo.com/v8/finance/chart/"
                         + quote(symbol, safe=""),
                         params={"period1": int(begin.timestamp()), "period2": int(end.timestamp()),
                                 "interval": "1d"},
                         headers={"User-Agent": "Mozilla/5.0 Hunter-DailyData/1.0"}).json()
    data = doc.get("chart") or {}
    if data.get("error") or not data.get("result"):
        raise RuntimeError("SOURCE_NO_CHART")
    return data["result"][0]


def sessions(market, after, now=None, source=chart):
    zone = ZoneInfo(ZONES[market])
    now = (now or dt.datetime.now(zone)).astimezone(zone)
    end = now.date() if now.time() >= dt.time(17, 30) else now.date() - dt.timedelta(days=1)
    begin = dt.date.fromisoformat(after) + dt.timedelta(days=1)
    if begin > end:
        return []
    data = source("^GSPC" if market == "US" else "^HSI", market, begin.isoformat(), end.isoformat())
    return sorted({d.isoformat() for ts in data.get("timestamp", [])
                   if begin <= (d := dt.datetime.fromtimestamp(ts, zone).date()) <= end})


def fetch_bar(security, date, source=chart):
    data = source(security["ticker"], security["market"], date, date)
    zone = ZoneInfo(ZONES[security["market"]])
    quote_rows = ((data.get("indicators") or {}).get("quote") or [{}])[0]
    found = []
    for i, ts in enumerate(data.get("timestamp", [])):
        if dt.datetime.fromtimestamp(ts, zone).date().isoformat() != date:
            continue
        values = {k: (quote_rows.get(k) or [])[i] if i < len(quote_rows.get(k) or []) else None
                  for k in ("open", "high", "low", "close", "volume")}
        if any(v is None for v in values.values()):
            continue
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in values.values()):
            raise RuntimeError("SOURCE_INVALID_NUMERIC")
        if security["market"] == "HK":
            prices = [values[k] for k in ("open", "high", "low", "close")]
            if values["volume"] == 0 and values["close"] > 0 and min(prices[:3]) <= 0:
                values["open"] = values["high"] = values["low"] = values["close"]
            elif min(prices) > 0:
                values["high"], values["low"] = max(prices), min(prices)
        if (min(values[k] for k in ("open", "high", "low", "close")) <= 0
                or values["volume"] < 0 or values["high"] < max(values["open"], values["close"], values["low"])
                or values["low"] > min(values["open"], values["close"])):
            raise RuntimeError("SOURCE_INVALID_BAR")
        found.append({"security_id": security["security_id"], "date": date,
                      "trade_date": date, "adjustment_as_of": date, **values})
    if len(found) > 1:
        raise RuntimeError("SOURCE_DUPLICATE_DAY")
    return found[0] if found else None


def identity(doc, market, date=None):
    if not isinstance(doc, dict) or doc.get("market") != market:
        raise RuntimeError("DAILY_CONTROL_IDENTITY_MISMATCH")
    if date is not None and doc.get("trade_date") != date:
        raise RuntimeError("DAILY_CONTROL_DATE_MISMATCH")


def process_date(drive, market, date, workers=6, fetch=fetch_bar):
    drive.target = date
    receipt = Record(drive, f"{market}/CONTROL/DAILY_RUN_{date}.json")
    if receipt.doc:
        identity(receipt.doc, market, date)
        if receipt.doc.get("status") in FINISHED:
            LOG.info("DAILY_DATA_DATE_REUSED market=%s date=%s", market, date)
            return receipt.doc
    progress = Record(drive, f"{market}/CONTROL/DAILY_DATA/{date}/STATE.json")
    if progress.doc is None:
        try:
            existing = drive.list(f"{market}/DAILY/{date}")
        except RuntimeError as exc:
            if "FOLDER_NOT_FOUND" not in str(exc):
                raise
            existing = []
        if existing:
            raise RuntimeError("TARGET_DAY_LEGACY_PARTIAL_REQUIRES_EXPLICIT_RECONCILIATION")
        universe = drive.json(f"{market}/CURRENT_UNIVERSE.json")
        identity(universe, market)
        active = [{k: s[k] for k in ("market", "security_id", "ticker")}
                  for s in universe["securities"] if s.get("listing_status", "ACTIVE") == "ACTIVE"]
        if (not active or any(s["market"] != market for s in active)
                or len({s["security_id"] for s in active}) != len(active)):
            raise RuntimeError("CURRENT_SYMBOLS_INVALID")
        progress.save({"schema": 1, "market": market, "trade_date": date,
                       "queue": active, "unavailable": [], "available": 0,
                       "active": len(active), "next_part": 1, "status": "RUNNING"})
    state = progress.doc
    identity(state, market, date)
    if state["status"] in FINISHED:
        result = summary(state)
        receipt.save(result)
        return result
    if state["status"] == "SOURCE_UNAVAILABLE":
        state["queue"], state["unavailable"] = state["unavailable"], []
        state["status"] = "RUNNING"
        progress.save(state)
    # Each committed batch is removed from queue; it is never fetched again.
    while state["queue"]:
        timeout(60)
        group = state["queue"][:100]
        part = state["next_part"]
        stage = Record(drive, f"{market}/CONTROL/DAILY_DATA/{date}/stage-{part:06d}.json")
        if stage.doc is None:
            def attempt(security):
                try:
                    return {"security": security, "row": fetch(security, date), "error": None}
                except Exception as exc:
                    return {"security": security, "row": None, "error": type(exc).__name__}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                items = list(pool.map(attempt, group))
            stage.save({"market": market, "trade_date": date, "items": items})
        identity(stage.doc, market, date)
        items = stage.doc["items"]
        if [i["security"] for i in items] != group:
            raise RuntimeError("PENDING_STAGE_IDENTITY_MISMATCH")
        rows = [i["row"] for i in items if i["row"] is not None]
        unavailable = [i["security"] for i in items if i["row"] is None]
        if rows:
            payload = lines_gz(rows)
            path = f"{market}/DAILY/{date}/part-data-{part:06d}.ndjson.gz"
            info = drive.file(path)
            if info:
                # Only this unfinished append is reconciled; no data readback.
                if info.get("md5") != hashlib.md5(payload).hexdigest() or int(info.get("size", -1)) != len(payload):
                    raise RuntimeError("PENDING_APPEND_CONFLICT")
            else:
                with saving():
                    drive.append(path, payload, "application/x-gzip")
        state["available"] += len(rows)
        state["unavailable"].extend(unavailable)
        state["queue"] = state["queue"][len(group):]
        state["next_part"] += 1
        progress.save(state)
        LOG.info("DAILY_DATA_PROGRESS market=%s date=%s processed=%d total=%d available=%d",
                 market, date, state["active"] - len(state["queue"]), state["active"], state["available"])
    # Preserve the established market-wide outage threshold, never call it success.
    if state["available"] / state["active"] <= .20:
        state["status"] = "SOURCE_UNAVAILABLE"
        progress.save(state)
        receipt.save(summary(state))
        raise RuntimeError("DAILY_DATA_SOURCE_UNAVAILABLE")
    state["status"] = "COMPLETE_WITH_GAPS" if state["unavailable"] else "COMPLETE"
    progress.save(state)
    result = summary(state)
    receipt.save(result)
    LOG.info("DAILY_DATA_COMPLETE market=%s date=%s available=%d active=%d gaps=%d ranking=DISABLED phase2=RETIRED",
             market, date, state["available"], state["active"], len(state["unavailable"]))
    return result


def summary(state):
    return {"market": state["market"], "trade_date": state["trade_date"],
            "status": state["status"], "active": state["active"], "available": state["available"],
            "missing_security_ids": [s["security_id"] for s in state["unavailable"]],
            "updated_at_myt": now_myt(), "pipeline": "DAILY_DATA_ONLY", "phase2": "RETIRED"}


def run(drive, market, workers=6, find_sessions=sessions, fetch=fetch_bar):
    pointer = Record(drive, f"{market}/CONTROL/DAILY_DATA_CHECKPOINT.json")
    if pointer.doc is None:
        legacy = drive.json(f"{market}/CONTROL/DAILY_CHECKPOINT.json")
        identity(legacy, market)
        dt.date.fromisoformat(legacy["last_completed_date"])
        pointer.save({"schema": 1, "market": market,
                      "last_completed_date": legacy["last_completed_date"], "pipeline": "DAILY_DATA_ONLY"})
    identity(pointer.doc, market)
    dates = find_sessions(market, pointer.doc["last_completed_date"])
    for date in dates:
        if date <= pointer.doc["last_completed_date"]:
            raise RuntimeError("DATE_REGRESSION_DENIED")
        result = process_date(drive, market, date, workers, fetch)
        pointer.save({"schema": 1, "market": market, "last_completed_date": date,
                      "updated_at_myt": now_myt(), "pipeline": "DAILY_DATA_ONLY", "coverage_status": result["status"]})
    LOG.info("DAILY_DATA_DONE market=%s date=%s sessions=%d phase2=RETIRED",
             market, pointer.doc["last_completed_date"], len(dates))
    return pointer.doc


def release_lease(drive, market, owner):
    record = Record(drive, f"{market}/CONTROL/DAILY_DATA_LEASE.json")
    if record.doc and record.doc.get("owner") == owner:
        record.save({"owner": owner, "expires": dt.datetime.now(dt.timezone.utc).isoformat()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=tuple(ZONES), required=True)
    # Existing Scheduler args are accepted but route exclusively to this module.
    parser.add_argument("--mode", choices=("auto", "daily", "daily-core", "daily-data"), default="daily-data")
    args = parser.parse_args()
    if os.getenv("HUNTER_ACTIONS_CUTOVER") != "CONFIRMED":
        raise RuntimeError("SINGLE_WRITER_CUTOVER_NOT_CONFIRMED")
    os.environ.update(HUNTER_CONTINUE_ONLY="1", HUNTER_READ_CHECKPOINTS="0", HUNTER_RESUMABLE_RUN="1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start()
    drive = DailyDrive(args.market)
    lease = Record(drive, f"{args.market}/CONTROL/DAILY_DATA_LEASE.json")
    now = dt.datetime.now(dt.timezone.utc)
    if lease.doc and dt.datetime.fromisoformat(lease.doc["expires"]) > now:
        raise RuntimeError("DAILY_DATA_ANOTHER_EXECUTION_ACTIVE")
    owner = os.getenv("CLOUD_RUN_EXECUTION") or str(uuid.uuid4())
    lease.save({"owner": owner, "expires": (now + dt.timedelta(seconds=7500)).isoformat()})
    try:
        run(drive, args.market, max(1, min(10, int(os.getenv("FETCH_WORKERS", "6")))))
    finally:
        cleaner = None
        try:
            # A lost data-write ACK must not prevent releasing our independent
            # control lease. A fresh transport may only release the same owner.
            with saving():
                cleaner = DailyDrive(args.market)
                release_lease(cleaner, args.market, owner)
        except Exception as exc:
            LOG.error("DAILY_DATA_LEASE_RELEASE_PENDING type=%s;lease_expires_automatically", type(exc).__name__)
        finally:
            if cleaner is not None:
                cleaner.http.close()
            drive.http.close()


if __name__ == "__main__":
    main()
