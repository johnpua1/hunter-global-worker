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
    proof = security.get("identity_proof")
    if (item["category"] == "IDENTITY_REVIEW" and
            item.get("problem") == "OFFICIAL_ISIN_SAME_NEW_TICKER" and
            market == "HK" and proof and proof.get("kind") == "HK_ISIN_MATCH" and
            proof.get("isin") == security.get("isin") and
            proof.get("new_ticker") == security["ticker"] and proof.get("source_hash")):
        return {**result, "result": "IDENTITY_FIXED", "accepted": True,
                "evidence": {**evidence, "official_identity_proof": proof}}
    official = security.get("official_listing_evidence")
    if (item["category"] == "IDENTITY_REVIEW" and official and
            security.get("identity_review") is False and
            official.get("ticker") == security["ticker"] and
            official.get("exchange") == security.get("exchange") and
            official.get("isin") == security.get("isin") and official.get("source_hash")):
        return {**result, "result": "IDENTITY_FIXED", "accepted": True,
                "evidence": {**evidence, "official_listing_evidence": official}}
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
    if item["category"] == "IDENTITY_REVIEW":
        return {**result, "result": "UNRESOLVED", "reason": "OFFICIAL_IDENTITY_PROOF_REQUIRED"}
    if not official or official.get("ticker") != security["ticker"] or not official.get("source_hash"):
        return {**result, "result": "UNRESOLVED", "reason": "OFFICIAL_LISTING_PROOF_REQUIRED"}
    state = load_market(drive, market)
    calendar = state.calendar
    if date and date > calendar[-1]:
        from runner import closed_dates_since
        calendar = calendar + closed_dates_since(market, calendar[-1])
    batch = item.get("batch")
    original = []
    if isinstance(batch, int):
        original = [r for r in parse_lines_gz(
            drive.read(f"{market}/BASE/batch-{batch:04d}.ndjson.gz")) if r["security_id"] == sid]
    suspect_days = set()
    if item["category"] == "DATA_SUSPECT" and not date:
        suspect_days = {r["date"] for r in original if
                        not (r["high"] >= max(r["open"], r["close"], r["low"]) and
                             r["low"] <= min(r["open"], r["close"]) and
                             min(r["open"], r["high"], r["low"], r["close"]) > 0 and
                             r["volume"] >= 0) or
                        (r["volume"] == 0 and len({r[k] for k in
                                                         ("open", "high", "low", "close")}) == 1)}
        if original:
            present = {r["date"] for r in original}
            first, last = min(present), max(present)
            suspect_days.update(d for d in calendar if first < d < last and d not in present)
        if not suspect_days:
            return {**result, "result": "UNRESOLVED", "reason": "SUSPECT_DATE_NOT_LOCALIZED"}
    rows, flags, splits, reason = fetch_security(security, calendar, calendar[-1], daily=True)
    if not rows:
        return {**result, "result": "UNRESOLVED", "reason": reason or "NO_YAHOO_BARS"}
    if date:
        rows = [row for row in rows if row["date"] == date]
    elif item["category"] == "DATA_SUSPECT":
        rows = [row for row in rows if row["date"] in suspect_days]
    else:
        if item["category"] != "FETCH_FAILED":
            return {**result, "result": "UNRESOLVED", "reason": "EXACT_DATE_REQUIRED"}
        if not isinstance(batch, int):
            return {**result, "result": "UNRESOLVED", "reason": "BASE_BATCH_UNKNOWN"}
        already = {r["date"] for r in original}
        rows = [row for row in rows if row["date"] not in already]
    if not rows:
        listing = security.get("listing_date")
        if date and listing and date < listing and security.get("listing_date_verified"):
            return {**result, "result": "SHORT_HISTORY", "reason": "PRE_LISTING_DATE"}
        return {**result, "result": "UNRESOLVED", "reason": "NO_BAR_AT_REQUESTED_DATE"}
    if any(not (r["high"] >= max(r["open"], r["close"], r["low"]) and
                r["low"] <= min(r["open"], r["close"]) and
                min(r["open"], r["high"], r["low"], r["close"]) > 0 and
                r["volume"] >= 0 and
                not (r["volume"] == 0 and len({r[k] for k in
                                                   ("open", "high", "low", "close")}) == 1))
           for r in rows):
        return {**result, "result": "UNRESOLVED", "reason": "BAR_STILL_SUSPECT"}
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


def run_repair(drive: Drive, market: str, limit: int = 150) -> dict:
    securities = {s["security_id"]: s for s in current_universe(drive, market)}
    raw = drive.read("REPAIR_QUEUE.json")
    doc = json.loads(raw)
    resolved = 0
    updates = {}
    accepted_count = 0
    # Revisit previously unresolved identity reviews once an official weekly
    # snapshot supplies proof. Keep fresh work ahead of repeated reviews.
    candidates = [x for x in doc["items"] if x.get("market") == market and
                  x.get("status", "OPEN") == "OPEN"]
    candidates += [x for x in doc["items"] if x.get("market") == market and
                   x.get("status") == "UNRESOLVED" and
                   x.get("reason") in ("OFFICIAL_IDENTITY_PROOF_REQUIRED",
                                       "OFFICIAL_LISTING_PROOF_REQUIRED") and
                   (securities.get(x.get("security_id")) or {}).get("official_listing_evidence") and
                   (x.get("category") != "IDENTITY_REVIEW" or
                    (securities[x["security_id"]].get("identity_review") is False and
                     securities[x["security_id"]]["official_listing_evidence"].get("ticker") ==
                     securities[x["security_id"]]["ticker"]))]
    for item in candidates:
        if resolved >= limit:
            break
        sid = item.get("security_id")
        if sid not in securities:
            item.update(status="UNRESOLVED", reason="SECURITY_ID_MISSING")
            resolved += 1
            updates[(sid, item.get("category"), item.get("trade_date"), item.get("batch"))] = dict(item)
            continue
        answer = decide(drive, item, securities[sid])
        if answer["accepted"]:
            accepted_count += 1
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
        updates[(sid, item.get("category"), item.get("trade_date"), item.get("batch"))] = dict(item)
    if resolved:
        for _ in range(5):
            latest = json.loads(raw)
            for item in latest["items"]:
                key = (item.get("security_id"), item.get("category"),
                       item.get("trade_date"), item.get("batch"))
                if item.get("market") == market and key in updates:
                    item.update(updates[key])
            try:
                drive.put("REPAIR_QUEUE.json", compact(latest), expected_sha=digest(raw))
                break
            except RuntimeError as exc:
                if "BRIDGE_STALE_WRITE" not in str(exc):
                    raise
                raw = drive.read("REPAIR_QUEUE.json")
        else:
            raise RuntimeError("REPAIR_QUEUE_CAS_EXHAUSTED")
    if accepted_count:
        from derived import build
        checkpoint = f"{market}/CONTROL/DAILY_CHECKPOINT.json"
        as_of = drive.json(checkpoint)["last_completed_date"] if drive.file(checkpoint) else load_market(
            drive, market).checkpoint["as_of"]
        build(drive, market, as_of)
    return {"market": market, "processed": resolved}
