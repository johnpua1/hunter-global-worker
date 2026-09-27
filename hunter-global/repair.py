"""Independent repair job: verify before accepting a historical sidecar."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
from collections import defaultdict
import time
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


def decide(drive: Drive, item: dict, security: dict, state=None, base_cache=None) -> dict:
    market, sid = item["market"], item["security_id"]
    evidence = {"ticker": security["ticker"], "exchange": security.get("exchange"),
                "listing_status": security.get("listing_status"),
                "listing_date": security.get("listing_date"),
                "official_source_hashes": security.get("source_hashes")}
    result = {"market": market, "security_id": sid,
              "category": item["category"], "accepted": False,
              "verified_at_myt": now_myt(), "evidence": evidence, "rows": []}
    delisting = security.get("delisting_evidence")
    if (delisting and delisting.get("official_source") and
            delisting.get("source_hash") and delisting.get("effective_date")):
        return {**result, "result": "DELISTED", "reason": "OFFICIAL_DELISTING_CONFIRMED",
                "evidence": {**evidence, "delisting": delisting}}
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
    if security.get("identity_review"):
        return {**result, "result": "UNRESOLVED", "reason": "IDENTITY_REVIEW_OPEN"}
    state = state or load_market(drive, market)
    calendar = state.calendar
    if date and date > calendar[-1]:
        from runner import closed_dates_since
        calendar = calendar + closed_dates_since(market, calendar[-1])
    batch = item.get("batch")
    original = []
    if isinstance(batch, int):
        if base_cache is not None:
            key = (market, batch)
            if key not in base_cache:
                by_id = defaultdict(list)
                for row in parse_lines_gz(drive.read(f"{market}/BASE/batch-{batch:04d}.ndjson.gz")):
                    by_id[row["security_id"]].append(row)
                base_cache[key] = by_id
            original = base_cache[key].get(sid, [])
        else:
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
        listing = security.get("listing_date")
        if date and listing and date < listing and security.get("listing_date_verified"):
            return {**result, "result": "SHORT_HISTORY", "reason": "PRE_LISTING_DATE"}
        if not official or not official.get("source_hash"):
            return {**result, "result": "UNRESOLVED", "reason": "OFFICIAL_LISTING_PROOF_REQUIRED"}
        try:
            secondary = second_source_close(security["ticker"], market)
        except Exception:
            return {**result, "result": "UNRESOLVED", "reason": "SECOND_SOURCE_UNAVAILABLE"}
        if date and date not in calendar:
            return {**result, "result": "UNRESOLVED", "reason": "SESSION_NOT_CONFIRMED"}
        if date and date in secondary:
            return {**result, "result": "UNRESOLVED", "reason": "PRIMARY_SOURCE_MISSING"}
        if not date and secondary:
            return {**result, "result": "UNRESOLVED", "reason": "PRIMARY_SOURCE_MISSING"}
        return {**result, "result": "NO_DATA", "reason": "TWO_INDEPENDENT_SOURCES_EMPTY",
                "evidence": {**evidence, "second_source": "stooq", "primary_error": reason}}
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


def run_repair(drive: Drive, market: str, *, deadline: float | None = None,
               chunk_size: int = 25) -> dict:
    """Persist each chunk before the Cloud Run deadline; restarts skip terminal rows."""
    if deadline is None:
        deadline = time.monotonic() + int(os.getenv("REPAIR_TIME_BUDGET_SECONDS", "3300"))
    securities = {s["security_id"]: s for s in current_universe(drive, market)}
    processed = accepted_count = 0
    state = None
    base_cache = {}
    while time.monotonic() < deadline - 300:
        raw = drive.read("REPAIR_QUEUE.json")
        doc = json.loads(raw)
        candidates = [(i, x) for i, x in enumerate(doc["items"])
                      if x.get("market") == market and x.get("status", "OPEN") == "OPEN"]
        if not candidates:
            break
        updates = {}
        for index, original in candidates[:chunk_size]:
            if time.monotonic() >= deadline - 300:
                break
            item = dict(original)
            sid = item.get("security_id")
            if sid not in securities:
                answer = {"result": "UNRESOLVED", "reason": "SECURITY_ID_MISSING_OR_EXECUTION_EVENT",
                          "verified_at_myt": now_myt(), "accepted": False}
            else:
                # BASE identity is immutable; read it once for this invocation.
                if state is None:
                    state = load_market(drive, market)
                answer = decide(drive, item, securities[sid], state, base_cache=base_cache)
            if answer["accepted"]:
                identity = digest(compact({"market": market, "security_id": sid,
                                           "category": item["category"], "batch": item.get("batch"),
                                           "trade_date": item.get("trade_date")}))[:24]
                # Shard repair sidecars so the Bridge's 1,000-entry folder
                # listing ceiling can never block DERIVED once the historical
                # backlog is drained. Existing flat sidecars remain readable.
                path = f"{market}/REPAIR_PATCH/{identity[:2]}/{identity}.json"
                if drive.file(path):
                    old = drive.json(path)
                    if not old.get("accepted") or old.get("result") != answer["result"]:
                        raise RuntimeError("PATCH_IDENTITY_CONFLICT:" + path)
                else:
                    drive.append(path, compact(answer))
                item["patch_path"] = path
                accepted_count += 1
            item.update(status=answer["result"], verified_at_myt=answer["verified_at_myt"],
                        reason=answer.get("reason"))
            updates[index] = item
        if not updates:
            break
        for _ in range(5):
            latest = json.loads(raw)
            for index, item in updates.items():
                if latest["items"][index].get("status", "OPEN") == "OPEN":
                    latest["items"][index] = item
            try:
                drive.put("REPAIR_QUEUE.json", compact(latest), expected_sha=digest(raw))
                break
            except RuntimeError as exc:
                if "BRIDGE_STALE_WRITE" not in str(exc):
                    raise
                raw = drive.read("REPAIR_QUEUE.json")
                if len(json.loads(raw)["items"]) < len(doc["items"]):
                    raise RuntimeError("REPAIR_QUEUE_SHRANK_DURING_CAS")
        else:
            raise RuntimeError("REPAIR_QUEUE_CAS_EXHAUSTED")
        processed += len(updates)
    remaining = sum(x.get("market") == market and x.get("status", "OPEN") == "OPEN"
                    for x in drive.json("REPAIR_QUEUE.json")["items"])
    return {"market": market, "processed": processed, "accepted": accepted_count,
            "open": remaining, "timed_out": remaining > 0 and time.monotonic() >= deadline - 300}
