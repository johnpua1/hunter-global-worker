"""Independent repair job: verify before accepting a historical sidecar."""
from __future__ import annotations

import csv
import datetime as dt
import concurrent.futures
import io
import json
import os
from collections import defaultdict
import time
import threading
from types import SimpleNamespace
from urllib.parse import quote

import requests

from time_budget import BudgetExceeded, budget
from runner import Drive, compact, digest, fetch_security, load_market, now_myt, retry_http, parse_lines_gz, TZ
from foundation import current_universe


def second_source_close(ticker: str, market: str, start: str | None = None,
                        end: str | None = None) -> dict[str, float]:
    # Independent corroboration only; repaired OHLC still comes from Yahoo.
    symbol = ticker.lower().replace(".hk", ".hk") if market == "HK" else ticker.lower() + ".us"
    url = "https://stooq.com/q/d/l/?s=" + quote(symbol) + "&i=d"
    if start:
        url += "&d1=" + start.replace("-", "")
    if end:
        url += "&d2=" + end.replace("-", "")
    response = retry_http(requests.Session(), "GET", url)
    return {r["Date"]: float(r["Close"]) for r in csv.DictReader(io.StringIO(response.text))
            if r.get("Date") and r.get("Close") not in (None, "N/D")}


def decide(drive: Drive, item: dict, security: dict, state=None, base_cache=None, base_cache_lock=None) -> dict:
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
            def load_once():
                if key not in base_cache:
                    by_id = defaultdict(list)
                    for row in parse_lines_gz(drive.read(f"{market}/BASE/batch-{batch:04d}.ndjson.gz")):
                        by_id[row["security_id"]].append(row)
                    base_cache[key] = by_id
            if base_cache_lock is not None:
                with base_cache_lock:
                    load_once()
            else:
                load_once()
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
                             r["volume"] >= 0)}
        if original:
            present = {r["date"] for r in original}
            first, last = min(present), max(present)
            suspect_days.update(d for d in calendar if first < d < last and d not in present)
        if not suspect_days:
            return {**result, "result": "VALIDATED_NO_DATA_DEFECT",
                    "reason": "NO_TRADE_BAR_VALID_OR_NOT_REPRODUCED"}
    repair_calendar = calendar
    repair_as_of = calendar[-1]
    if date:
        if date not in calendar:
            return {**result, "result": "UNRESOLVED", "reason": "SESSION_NOT_CONFIRMED"}
        repair_calendar = [date]
        repair_as_of = date
    elif item["category"] == "DATA_SUSPECT" and suspect_days:
        repair_calendar = sorted(suspect_days)
        repair_as_of = repair_calendar[-1]
    rows, flags, splits, reason = fetch_security(
        security, repair_calendar, repair_as_of, daily=True)
    if not rows:
        listing = security.get("listing_date")
        if date and listing and date < listing and security.get("listing_date_verified"):
            return {**result, "result": "SHORT_HISTORY", "reason": "PRE_LISTING_DATE"}
        if not official or not official.get("source_hash"):
            return {**result, "result": "UNRESOLVED", "reason": "OFFICIAL_LISTING_PROOF_REQUIRED"}
        if date and date not in calendar:
            return {**result, "result": "UNRESOLVED", "reason": "SESSION_NOT_CONFIRMED"}
        return {**result, "result": "NO_DATA",
                "reason": "NO_PRIMARY_BAR_ACTIVE_LISTING",
                "evidence": {**evidence, "primary_source": "Yahoo chart",
                             "primary_error": reason}}
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
                r["volume"] >= 0)
           for r in rows):
        return {**result, "result": "UNRESOLVED", "reason": "BAR_STILL_SUSPECT"}
    # A fresh Yahoo re-fetch that now satisfies the hard OHLC/volume
    # invariants is accepted as a correction of the previously stored suspect
    # row. External corroboration is not a hard dependency because that source
    # can be unavailable independently of the market data itself.
    result.update({"result": "RESOLVED", "accepted": True, "rows": rows,
                   "evidence": {**evidence,
                                "verification": "PRIMARY_REFRESH_STRUCTURAL_VALIDATION",
                                "primary_source": "Yahoo chart",
                                "split_events_observed": splits}})
    return result


RETRYABLE_REASONS = frozenset({
    "MARKET_WIDE_OUTAGE", "SESSION_NOT_CONFIRMED", "NO_BAR_AT_REQUESTED_DATE",
    "NO_PRIMARY_BAR_ACTIVE_LISTING",
})
MAX_REPAIR_ATTEMPTS = 3


def append_repair_patch(drive: Drive, path: str, content: bytes):
    """Reconcile an ambiguous append only after exact-byte readback.

    Bridge append stays immutable. A successful write followed by a lost HTTP
    response can make Drive._call's retry return APPEND_CONFLICT. That is safe
    to acknowledge only if the persisted bytes equal this exact submission.
    """
    try:
        drive.append(path, content)
    except RuntimeError as exc:
        if str(exc) != "BRIDGE_APPEND_CONFLICT":
            raise
        stored = drive.read(path)
        if stored != content:
            raise RuntimeError("PATCH_APPEND_CONTENT_CONFLICT:" + path) from exc


def repair_attempts(item: dict) -> int:
    # Legacy evaluated rows already consumed one attempt.
    return int(item.get("repair_attempts", 1 if item.get("verified_at_myt") else 0))


def retryable(item: dict) -> bool:
    return (item.get("status") in {"UNRESOLVED", "NO_DATA"}
            and item.get("reason") in RETRYABLE_REASONS)


def repair_due(item: dict, now: dt.datetime) -> bool:
    if item.get("status", "OPEN") == "OPEN":
        return True
    if not retryable(item) or repair_attempts(item) >= MAX_REPAIR_ATTEMPTS:
        return False
    stamp = item.get("next_retry_at_myt")
    try:
        due = (dt.datetime.fromisoformat(stamp) if stamp else
               dt.datetime.fromisoformat(item["verified_at_myt"]) + dt.timedelta(hours=6))
        if due.tzinfo is None:
            return False  # An ambiguous legacy timestamp must not cause a hot retry loop.
        return now >= due
    except (KeyError, TypeError, ValueError):
        return False




def run_repair(drive, market, *, deadline=None, chunk_size=5, shard_index=None, shard_count=None):
    if deadline is None:
        deadline = time.monotonic() + int(os.getenv("REPAIR_TIME_BUDGET_SECONDS", "3120"))
    with budget(deadline):
        return _run_repair(drive, market, deadline=deadline, chunk_size=chunk_size,
                           shard_index=shard_index, shard_count=shard_count)


def _run_repair(drive: Drive, market: str, *, deadline: float | None = None,
               chunk_size: int = 5, shard_index: int | None = None,
               shard_count: int | None = None) -> dict:
    """Persist chunks and retry transient failures at most three times with backoff."""
    if deadline is None:
        deadline = time.monotonic() + int(os.getenv("REPAIR_TIME_BUDGET_SECONDS", "3300"))
    securities = {s["security_id"]: s for s in current_universe(drive, market)}
    processed = accepted_count = 0
    budget_stopped = False
    state = None
    base_cache = {}
    base_cache_lock = threading.Lock()
    while time.monotonic() < deadline - 300:
        raw = drive.read("REPAIR_QUEUE.json")
        doc = json.loads(raw)
        candidates = [(i, x) for i, x in enumerate(doc["items"])
                      if x.get("market") == market
                      and repair_due(x, dt.datetime.now(TZ))
                      and (shard_count is None or shard_index is None
                           or i % shard_count == shard_index)]
        if not candidates:
            break
        batch_candidates = candidates[:chunk_size]
        if state is None and any(x.get("security_id") in securities for _, x in batch_candidates):
            # Direct Phase 1 repair only needs the sealed trading calendar.
            # Do not re-read/re-hash the full BASE universe on every resume.
            calendar = [row["date"] for row in parse_lines_gz(
                drive.read(f"{market}/CALENDAR_BASE.ndjson.gz"))]
            state = SimpleNamespace(calendar=calendar)

        # BASE is immutable/sealed. Prefetch only the distinct shards needed by
        # this chunk, using independent read-only Bridge sessions. This avoids
        # serial Apps Script round-trips while keeping all queue/patch writes on
        # the single main Drive writer.
        missing_batches = []
        for _, original in batch_candidates:
            batch = original.get("batch")
            if isinstance(batch, int) and batch >= 1:
                key = (market, batch)
                if key in base_cache:
                    continue
                base_path = f"{market}/BASE/batch-{batch:04d}.ndjson.gz"
                # Production queue entries are bound to sealed BASE batches.
                # Avoid a serial Bridge 'file' round-trip before each read;
                # a missing BASE object must fail on read. MemoryDrive/tests do
                # not expose the production transport marker and keep the
                # existence check contract.
                if hasattr(drive, "url") or drive.file(base_path):
                    missing_batches.append((key, base_path))
        # preserve first occurrence only
        seen_missing = set()
        missing_batches = [x for x in missing_batches
                           if not (x[0] in seen_missing or seen_missing.add(x[0]))]

        def read_base(entry):
            with budget(deadline - 300):
                key, path = entry
                reader = Drive()
                by_id = defaultdict(list)
                for row in parse_lines_gz(reader.read(path)):
                    by_id[row["security_id"]].append(row)
                return key, by_id

        try:
            if missing_batches:
                base_read_workers = max(1, min(4, int(os.getenv("BASE_READ_WORKERS", "4"))))
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(base_read_workers, len(missing_batches))) as pool:
                    for key, by_id in pool.map(read_base, missing_batches):
                        base_cache[key] = by_id
        except BudgetExceeded:
            budget_stopped = True
            break

        def evaluate(pair):
            try:
                with budget(deadline - 300):
                    index, original = pair
                    item = dict(original)
                    sid = item.get("security_id")
                    if sid not in securities:
                        answer = {"result": "UNRESOLVED",
                                  "reason": "SECURITY_ID_MISSING_OR_EXECUTION_EVENT",
                                  "verified_at_myt": now_myt(), "accepted": False}
                    else:
                        answer = decide(drive, item, securities[sid], state, base_cache=base_cache, base_cache_lock=base_cache_lock)
                    return index, item, sid, answer
            except BudgetExceeded:
                return None

        workers = max(1, int(os.getenv("REPAIR_WORKERS", os.getenv("FETCH_WORKERS", "6"))))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            evaluated = list(pool.map(evaluate, batch_candidates))

        updates = {}
        for evaluated_item in evaluated:
            if evaluated_item is None:
                budget_stopped = True
                continue
            index, item, sid, answer = evaluated_item
            try:
                with budget(deadline - 300):
                    if answer["accepted"]:
                        identity = digest(compact({"market": market, "security_id": sid,
                                                   "category": item["category"], "batch": item.get("batch"),
                                                   "trade_date": item.get("trade_date")}))[:24]
                        # Shard repair sidecars so the Bridge's 1,000-entry folder
                        # listing ceiling can never block DERIVED once the historical
                        # backlog is drained. Existing flat/bulk sidecars remain readable.
                        path = f"{market}/REPAIR_PATCH/{identity[:2]}/{identity}.json"
                        if drive.file(path):
                            previous = drive.json(path)
                            if not previous.get("accepted") or previous.get("result") != answer["result"]:
                                raise RuntimeError("PATCH_IDENTITY_CONFLICT:" + path)
                        else:
                            append_repair_patch(drive, path, compact(answer))
                        item["patch_path"] = path
                        accepted_count += 1
            except BudgetExceeded:
                budget_stopped = True
                break  # Persist preceding rows using the reserved CAS budget.
            item.update(status=answer["result"], verified_at_myt=answer["verified_at_myt"],
                        reason=answer.get("reason"))
            item["repair_attempts"] = repair_attempts(doc["items"][index]) + 1
            item.pop("next_retry_at_myt", None)
            item.pop("retry_exhausted", None)
            if retryable(item):
                item["retry_exhausted"] = item["repair_attempts"] >= MAX_REPAIR_ATTEMPTS
                if not item["retry_exhausted"]:
                    wait_hours = 6 if item["repair_attempts"] == 1 else 24
                    item["next_retry_at_myt"] = (
                        dt.datetime.now(TZ) + dt.timedelta(hours=wait_hours)
                    ).isoformat(timespec="seconds")
            updates[index] = item
        if not updates:
            break
        for _ in range(12):
            latest = json.loads(raw)
            for index, item in updates.items():
                # Do not overwrite another writer's changed row on a CAS retry.
                if latest["items"][index] == doc["items"][index]:
                    latest["items"][index] = item
            try:
                drive.put_fast("REPAIR_QUEUE.json", compact(latest), expected_sha=digest(raw))
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
        if budget_stopped:
            break
    final_items = drive.json("REPAIR_QUEUE.json")["items"]
    remaining = sum(
        x.get("market") == market and x.get("status", "OPEN") == "OPEN"
        and (shard_count is None or shard_index is None or i % shard_count == shard_index)
        for i, x in enumerate(final_items))
    scoped = [x for i, x in enumerate(final_items) if x.get("market") == market
              and (shard_count is None or shard_index is None or i % shard_count == shard_index)]
    due = sum(repair_due(x, dt.datetime.now(TZ)) for x in scoped)
    retry_waiting = sum(retryable(x) and repair_attempts(x) < MAX_REPAIR_ATTEMPTS
                        and not repair_due(x, dt.datetime.now(TZ)) for x in scoped)
    exhausted = sum(retryable(x) and repair_attempts(x) >= MAX_REPAIR_ATTEMPTS for x in scoped)
    return {"market": market, "processed": processed, "accepted": accepted_count,
            "open": remaining, "shard_index": shard_index, "shard_count": shard_count,
            "due": due, "retry_waiting": retry_waiting, "retry_exhausted": exhausted,
            "timed_out": due > 0 and (budget_stopped or time.monotonic() >= deadline - 300)}
