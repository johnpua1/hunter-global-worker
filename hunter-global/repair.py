"""Independent repair job: verify before accepting a historical sidecar."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from urllib.parse import quote

import requests

from runner import Drive, compact, digest, fetch_security, load_market, now_myt, retry_http, parse_lines_gz
from foundation import current_universe


def second_source_close(ticker: str, market: str) -> dict[str, float]:
    # Independent corroboration only; repaired OHLC still comes from Yahoo.
    symbol = ticker.lower().replace(".hk", ".hk") if market == "HK" else ticker.lower() + ".us"
    response = retry_http(requests.Session(), "GET",
                          "https://stooq.com/q/d/l/?s=" + quote(symbol) + "&i=d")
    return {r["Date"]: float(r["Close"]) for r in csv.DictReader(io.StringIO(response.text))
            if r.get("Date") and r.get("Close") not in (None, "N/D")}


def decide(drive: Drive, item: dict, security: dict) -> dict:
    market, sid = item["market"], item["security_id"]
    evidence = {"ticker": security["ticker"], "exchange": security.get("exchange"),
                "listing_status": security.get("listing_status"),
                "listing_date": security.get("listing_date"),
                "official_source_hashes": security.get("source_hashes")}
    result = {"market": market, "security_id": sid,
              "category": item["category"], "accepted": False,
              "verified_at_myt": now_myt(), "evidence": evidence, "rows": []}
    date = item.get("trade_date")
    if date:
        outage = f"{market}/CONTROL/DAILY_RUN_{date}.json"
        if drive.file(outage) and drive.json(outage).get("status") == "MARKET_WIDE_DATA_UNAVAILABLE":
            return {**result, "result": "UNRESOLVED", "reason": "MARKET_WIDE_OUTAGE"}
    if item["category"] == "SUSPECT_DELISTED":
        # A 404 or absent listing snapshot does not establish a delisting.
        return {**result, "result": "UNRESOLVED", "reason": "OFFICIAL_DELISTING_PROOF_REQUIRED"}
    if not evidence["ticker"] or not evidence["exchange"]:
        return {**result, "result": "UNRESOLVED", "reason": "IDENTITY_NOT_VERIFIED"}
    state = load_market(drive, market)
    calendar = state.calendar
    if date and date > calendar[-1]:
        from runner import closed_dates_since
        calendar = calendar + closed_dates_since(market, calendar[-1])
    rows, flags, splits, reason = fetch_security(security, calendar, calendar[-1], daily=True)
    if not rows:
        return {**result, "result": "UNRESOLVED", "reason": reason or "NO_YAHOO_BARS"}
    if "DATA_SUSPECT" in flags:
        return {**result, "result": "UNRESOLVED", "reason": "BAR_STILL_SUSPECT"}
    if date:
        rows = [row for row in rows if row["date"] == date]
    else:
        if item["category"] != "FETCH_FAILED":
            return {**result, "result": "UNRESOLVED", "reason": "EXACT_DATE_REQUIRED"}
        batch = item.get("batch")
        if not isinstance(batch, int):
            return {**result, "result": "UNRESOLVED", "reason": "BASE_BATCH_UNKNOWN"}
        original = parse_lines_gz(drive.read(f"{market}/BASE/batch-{batch:04d}.ndjson.gz"))
        already = {r["date"] for r in original if r["security_id"] == sid}
        rows = [row for row in rows if row["date"] not in already]
    if not rows:
        return {**result, "result": "SHORT_HISTORY", "reason": "NO_BAR_AT_REQUESTED_DATE"}
    try:
        secondary = second_source_close(security["ticker"], market)
    except Exception as exc:
        return {**result, "result": "UNRESOLVED", "reason": "SECOND_SOURCE_UNAVAILABLE:" + type(exc).__name__}
    if any(row["date"] not in secondary or
           abs(row["close"] / secondary[row["date"]] - 1) > .02 for row in rows):
        return {**result, "result": "UNRESOLVED", "reason": "SECOND_SOURCE_MISMATCH"}
    # Corporate actions are recorded separately and applied by the query
    # layer. Raw historical bars remain at their original adjustment anchor.
    result.update({"result": "RESOLVED", "accepted": True, "rows": rows,
                   "evidence": {**evidence, "second_source": "stooq",
                                "split_events_observed": splits}})
    return result


def run_repair(drive: Drive, market: str, limit: int = 20) -> dict:
    securities = {s["security_id"]: s for s in current_universe(drive, market)}
    raw = drive.read("REPAIR_QUEUE.json")
    doc = json.loads(raw)
    resolved = 0
    for item in doc["items"]:
        if item.get("market") != market or item.get("status", "OPEN") != "OPEN":
            continue
        if resolved >= limit:
            break
        sid = item.get("security_id")
        if sid not in securities:
            item.update(status="UNRESOLVED", reason="SECURITY_ID_MISSING")
            resolved += 1
            continue
        answer = decide(drive, item, securities[sid])
        if answer["accepted"]:
            identity = digest(compact({"market": market, "security_id": sid,
                                       "category": item["category"],
                                       "trade_date": item.get("trade_date")}))[:24]
            path = f"{market}/REPAIR_PATCH/{identity}.json"
            if drive.file(path):
                if drive.json(path) != answer:
                    # Timestamp differs after restart; accept the already
                    # persisted verified patch rather than replacing it.
                    old = drive.json(path)
                    if not old.get("accepted") or old.get("result") != answer["result"]:
                        raise RuntimeError("PATCH_IDENTITY_CONFLICT:" + path)
            else:
                drive.append(path, compact(answer))
            item["patch_path"] = path
        item["status"] = answer["result"]
        item["verified_at_myt"] = answer["verified_at_myt"]
        item["reason"] = answer.get("reason")
        resolved += 1
    if resolved:
        drive.put("REPAIR_QUEUE.json", compact(doc), expected_sha=digest(raw))
    return {"market": market, "processed": resolved}
