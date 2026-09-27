"""Resume the US/HK Hunter build from the existing Drive checkpoint.

No Drive mutation occurs in probe mode. Never run base mode while either
Apps Script trigger is enabled: Apps Script does not honor this worker's lock.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests


ROOT = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TZ = ZoneInfo("Asia/Kuala_Lumpur")
LOG = logging.getLogger("hunter")
MARKETS = ("US", "HK")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compact(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def lines_gz(rows: list[dict]) -> bytes:
    # Apps Script produces gzip, and its manifest checks the compressed bytes.
    payload = b"\n".join(compact(row) for row in rows) + (b"\n" if rows else b"")
    return gzip.compress(payload, mtime=0)


def parse_lines_gz(data: bytes) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(data).splitlines() if line]


def now_myt() -> str:
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def retry_http(session, method: str, url: str, **kwargs):
    retryable = {429, 500, 502, 503, 504}
    last_error = None
    attempts = max(1, int(os.getenv("HUNTER_HTTP_RETRY_ATTEMPTS", "5")))
    timeout = max(3.0, float(os.getenv("HUNTER_HTTP_TIMEOUT_SECONDS", "45")))
    for attempt in range(attempts):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
            time.sleep(min(30, 2**attempt + random.random()))
            continue

        # Non-retryable HTTP failures (for example 401/403/404) are
        # deterministic for this request and must fail immediately. Only
        # throttling/transient server statuses are retried.
        if response.status_code not in retryable:
            response.raise_for_status()
            return response

        last_error = requests.HTTPError(f"HTTP {response.status_code}", response=response)
        if attempt == attempts - 1:
            raise last_error
        time.sleep(min(30, 2**attempt + random.random()))

    if last_error is not None:
        raise last_error
    raise AssertionError("unreachable")


class Drive:
    """Restricted Apps Script transport for this Hunter worker."""

    def __init__(self):
        needed = ("APPS_SCRIPT_WEBAPP_URL", "APPS_SCRIPT_SHARED_KEY")
        missing = [key for key in needed if not os.environ.get(key)]
        if missing:
            raise RuntimeError("BRIDGE_AUTH_MISSING:" + ",".join(missing))
        self.url = os.environ["APPS_SCRIPT_WEBAPP_URL"]
        if not (self.url.startswith("https://script.google.com/macros/s/") and self.url.endswith("/exec")):
            raise RuntimeError("BRIDGE_URL_INVALID")
        self.key = os.environ["APPS_SCRIPT_SHARED_KEY"]
        self.http = requests.Session()
        self.folders: dict[str, str] = {"": ""}

    def health(self):
        expected = {"ok": True, "service": "HUNTER_GLOBAL_BRIDGE"}
        for attempt in range(8):
            try:
                response = self.http.get(self.url, timeout=60)
                response.raise_for_status()
                result = response.json()
                if result != expected:
                    raise ValueError("BRIDGE_HEALTH_RESPONSE_SHAPE")
                LOG.info("bridge health ok")
                return
            except (requests.RequestException, ValueError):
                if attempt == 7:
                    raise
                # Apps Script redirects health GETs to short-lived
                # script.googleusercontent.com URLs.  A stale redirect can
                # transiently return 404; rebuild the session and retry the
                # original /exec URL instead of accepting or caching it.
                self.http.close()
                self.http = requests.Session()
                time.sleep(min(30, 2 ** attempt + random.random()))
        raise AssertionError("unreachable")

    def _call(self, op: str, **fields) -> dict:
        request = {"op": op, "key": self.key, **fields}
        attempts = max(1, int(os.getenv("HUNTER_BRIDGE_ATTEMPTS", "8")))
        for attempt in range(attempts):
            try:
                if op == "read":
                    timeout = float(os.getenv("HUNTER_BRIDGE_READ_TIMEOUT_SECONDS", "30"))
                else:
                    timeout = float(os.getenv("HUNTER_BRIDGE_WRITE_TIMEOUT_SECONDS", "120"))
                response = self.http.post(self.url, json=request, timeout=timeout)
                response.raise_for_status()
                result = response.json()
                # Apps Script can rarely return the doGet health payload to a
                # POST route during a transient redirect/session anomaly. Never
                # accept it as operation data; reset the HTTP session and retry.
                if result == {"ok": True, "service": "HUNTER_GLOBAL_BRIDGE"}:
                    self.http.close()
                    self.http = requests.Session()
                    raise ValueError("BRIDGE_POST_RETURNED_HEALTH:" + op)
                if not result.get("ok"):
                    raise RuntimeError("BRIDGE_" + str(result.get("error", "UNKNOWN")))
                required = {"read": ("data_base64", "sha256"), "put": ("file", "sha256"),
                            "append": ("file", "sha256"),
                            "list": ("files",), "file": ("file",), "folder": ("folder",)}
                if not all(field in result for field in required[op]):
                    raise ValueError("BRIDGE_RESPONSE_SHAPE:" + op + ":" + ",".join(sorted(result)))
                return result
            except (requests.RequestException, ValueError):
                if attempt == attempts - 1:
                    raise
                time.sleep(min(30, 2 ** min(attempt, 4) + random.random()))
        raise AssertionError("unreachable")

    def list(self, parent_id: str, name: str | None = None) -> list[dict]:
        return self._call("list", path=parent_id, name=name)["files"]

    def folder(self, path: str, create: bool = False) -> str:
        path = path.strip("/")
        if path in self.folders:
            return path
        self._call("folder", path=path, create=create)
        self.folders[path] = path
        return path

    def file(self, path: str) -> dict | None:
        return self._call("file", path=path.strip("/")).get("file")

    def read(self, path: str) -> bytes:
        import base64
        result = self._call("read", path=path.strip("/"))
        data = base64.b64decode(result["data_base64"], validate=True)
        if digest(data) != result["sha256"]:
            raise RuntimeError("BRIDGE_READ_SHA_MISMATCH:" + path)
        return data

    def json(self, path: str) -> Any:
        return json.loads(self.read(path))

    def put(self, path: str, content: bytes, mime: str = "application/json", immutable=False, expected_sha=None):
        import base64
        fields = {"path": path.strip("/"), "data_base64": base64.b64encode(content).decode("ascii"),
                  "sha256": digest(content), "mime": mime, "immutable": immutable}
        if expected_sha is not None:
            fields["expected_sha256"] = expected_sha
        result = self._call("put", **fields)
        if result["sha256"] != digest(content):
            raise RuntimeError("BRIDGE_WRITE_SHA_MISMATCH:" + path)
        if digest(self.read(path)) != digest(content):
            raise RuntimeError("DRIVE_READBACK_MISMATCH:" + path)
        return result["file"]

    def put_fast(self, path: str, content: bytes, mime: str = "application/json", expected_sha=None):
        """CAS write with Bridge SHA confirmation, without an immediate full-file readback."""
        import base64
        fields = {"path": path.strip("/"), "data_base64": base64.b64encode(content).decode("ascii"),
                  "sha256": digest(content), "mime": mime, "immutable": False}
        if expected_sha is not None:
            fields["expected_sha256"] = expected_sha
        result = self._call("put", **fields)
        if result["sha256"] != digest(content):
            raise RuntimeError("BRIDGE_WRITE_SHA_MISMATCH:" + path)
        return result["file"]

    def append(self, path: str, content: bytes, mime: str = "application/json"):
        """Create a segment once; the bridge rejects equal-byte rewrites too."""
        import base64
        result = self._call("append", path=path.strip("/"),
                            data_base64=base64.b64encode(content).decode("ascii"),
                            sha256=digest(content), mime=mime)
        if result["sha256"] != digest(content) or digest(self.read(path)) != digest(content):
            raise RuntimeError("APPEND_READBACK_MISMATCH:" + path)
        return result["file"]


    def append_repairs(self, market: str, batch: int, statuses: list[dict], reasons: dict):
        flags = {"FETCH_FAILED", "DATA_SUSPECT", "IDENTITY_REVIEW"}
        additions = [
            {"category": flag, "market": market, "security_id": entry["security_id"],
             "batch": batch, "problem": reasons.get(entry["security_id"], flag) if flag == "FETCH_FAILED" else flag,
             "attempted_fixes": [], "recorded_at_myt": now_myt()}
            for entry in statuses for flag in entry["status"] if flag in flags
        ]
        if not additions:
            return
        path = "REPAIR_QUEUE.json"
        while True:
            raw = self.read(path)
            document = json.loads(raw)
            if not isinstance(document, dict) or not isinstance(document.get("items"), list):
                raise RuntimeError("REPAIR_QUEUE_INVALID")
            existing = {(x.get("market"), x.get("security_id"), x.get("batch"), x.get("category"))
                        for x in document["items"] if isinstance(x, dict)}
            new = [x for x in additions if (x["market"], x["security_id"], x["batch"], x["category"]) not in existing]
            if not new:
                return
            document["items"].extend(new)
            try:
                self.put(path, compact(document), expected_sha=digest(raw))
                LOG.info("repair queue market=%s batch=%04d added=%d", market, batch, len(new))
                return
            except RuntimeError as exc:
                if "BRIDGE_STALE_WRITE" not in str(exc):
                    raise
                time.sleep(1 + random.random())


def yahoo_chart(symbol: str, start_date: str, end_date: str) -> dict:
    first = int(dt.datetime.fromisoformat(start_date).replace(tzinfo=dt.timezone.utc).timestamp()) - 5 * 86400
    # Query split events through today: Yahoo may retroactively reflect a
    # split after the immutable base AS_OF in older bars.
    event_end = max(dt.date.fromisoformat(end_date), dt.datetime.now(TZ).date())
    last = int(dt.datetime.combine(event_end, dt.time(), dt.timezone.utc).timestamp()) + 2 * 86400
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="")
    response = retry_http(
        requests.Session(), "GET", url,
        params={"period1": first, "period2": last, "interval": "1d", "events": "splits"},
        headers={"User-Agent": "Mozilla/5.0 Hunter/1.0", "Accept": "application/json"},
    ).json()
    chart = response.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise ValueError("YAHOO_NO_CHART:" + symbol)
    return chart["result"][0]


def fetch_security(security: dict, calendar: list[str], as_of: str,
                   daily: bool = False) -> tuple[list[dict], list[str], list[dict], str | None]:
    sid = security["security_id"]
    market = security["market"]
    symbol = security["ticker"]
    try:
        data = yahoo_chart(symbol, calendar[0], as_of)
        quote_data = ((data.get("indicators") or {}).get("quote") or [{}])[0]
        meta = data.get("meta") or {}
        offset = int(meta.get("gmtoffset") or (8 * 3600 if market == "HK" else -4 * 3600))
        dates = [dt.datetime.fromtimestamp(ts + offset, dt.timezone.utc).date().isoformat()
                 for ts in data.get("timestamp", [])]
        rows = []
        allowed = set(calendar)
        for i, date in enumerate(dates):
            if date not in allowed or date > as_of:
                continue
            vals = {key: (quote_data.get(key) or [None] * len(dates))[i]
                    for key in ("open", "high", "low", "close", "volume")}
            if any(vals[key] is None for key in vals):
                continue
            if market == "HK":
                # Yahoo HK daily bars can combine auction open/close prices with
                # continuous-session high/low fields. Canonicalize the full-day
                # envelope so H/L contains every observed same-day price.
                prices = [float(vals[k]) for k in ("open", "high", "low", "close")]
                if vals["volume"] == 0 and float(vals["close"]) > 0 and any(x <= 0 for x in prices[:3]):
                    vals["open"] = vals["high"] = vals["low"] = vals["close"]
                elif min(prices) > 0:
                    vals["high"] = max(prices)
                    vals["low"] = min(prices)
            rows.append({"security_id": sid, "date": date, **vals, "adjustment_as_of": as_of})
        rows.sort(key=lambda r: r["date"])
        seen = {r["date"] for r in rows}
        flags = []
        if security.get("identity_review"):
            flags.append("IDENTITY_REVIEW")
        if not rows:
            return [], flags + ["FETCH_FAILED"], [], "NO_ROWS"
        if any(not (r["high"] >= max(r["open"], r["close"], r["low"]) and
                    r["low"] <= min(r["open"], r["close"]) and
                    min(r["open"], r["high"], r["low"], r["close"]) > 0 and
                    r["volume"] >= 0) for r in rows) or len(seen) != len(rows):
            flags.append("DATA_SUSPECT")
        if len(rows) == len(calendar) and [r["date"] for r in rows] == calendar:
            flags.append("PASS_DAILY" if daily else "PASS_501")
        else:
            flags.append("MISSING_DAILY" if daily else "INSUFFICIENT_HISTORY_NEW_LISTING")
            if any(date not in seen for date in calendar if rows[0]["date"] < date < rows[-1]["date"]):
                if "DATA_SUSPECT" not in flags:
                    flags.insert(0, "DATA_SUSPECT")
        for before, after in zip(rows, rows[1:]):
            if before["close"] and abs(after["close"] / before["close"] - 1) > 0.5:
                if "LARGE_MOVE" not in flags:
                    flags.insert(0, "LARGE_MOVE")
        splits = []
        for event in ((data.get("events") or {}).get("splits") or {}).values():
            date = event.get("date")
            factor = event.get("numerator", 0) / max(1, event.get("denominator", 0))
            if date and factor > 0:
                split_date = dt.datetime.fromtimestamp(date + offset, dt.timezone.utc).date().isoformat()
                splits.append({"security_id": sid, "effective_date": split_date, "factor": factor, "ticker": symbol})
                if split_date > as_of:
                    for row in rows:
                        if row["date"] < split_date:
                            for key in ("open", "high", "low", "close"):
                                row[key] *= factor
                            row["volume"] /= factor
        return rows, flags, splits, None
    except Exception as exc:
        return [], ["FETCH_FAILED"], [], f"{type(exc).__name__}:{str(exc)[:180]}"


@dataclass
class MarketState:
    name: str
    checkpoint: dict
    securities: list[dict]
    calendar: list[str]


def load_market(drive: Drive, market: str) -> MarketState:
    checkpoint = drive.json(f"{market}/CHECKPOINT.json")
    as_of = checkpoint["as_of"]
    universe = parse_lines_gz(drive.read(f"{market}/UNIVERSE_BASE_{as_of}.ndjson.gz"))
    calendar = [row["date"] for row in parse_lines_gz(drive.read(f"{market}/CALENDAR_BASE.ndjson.gz"))]
    if len(universe) != checkpoint["target"] or len(calendar) != 501:
        raise RuntimeError(f"BASE_IDENTITY_MISMATCH:{market}")
    if digest(drive.read(f"{market}/UNIVERSE_BASE_{as_of}.ndjson.gz")) != checkpoint["universe_sha256"]:
        raise RuntimeError(f"UNIVERSE_SHA_MISMATCH:{market}")
    if digest(drive.read(f"{market}/CALENDAR_BASE.ndjson.gz")) != checkpoint["calendar_sha256"]:
        raise RuntimeError(f"CALENDAR_SHA_MISMATCH:{market}")
    return MarketState(market, checkpoint, universe, calendar)


def verified_receipt(drive: Drive, market: str, batch: int) -> dict | None:
    path = f"{market}/VERIFIED/batch-{batch:04d}.json"
    return drive.json(path) if drive.file(path) else None


def verified_integrity(drive: Drive, market: str, batch: int, receipt: dict) -> bool:
    stem = f"{market}/BASE/batch-{batch:04d}"
    status = f"{market}/STATUS/batch-{batch:04d}.status.json"
    try:
        payload = drive.read(stem + ".ndjson.gz")
        manifest = drive.read(stem + ".manifest.json")
        statuses = drive.read(status)
        info = json.loads(manifest)
        return (
            len(payload) == receipt["bytes"] and digest(payload) == receipt["sha256"]
            and digest(manifest) == receipt["manifest_sha256"]
            and digest(statuses) == receipt["status_sha256"]
            and len(parse_lines_gz(payload)) == info["row_count"]
            and len(json.loads(statuses)) == info["security_count"]
        )
    except (KeyError, ValueError, FileNotFoundError, OSError):
        return False


def batch_result(state: MarketState, batch: int, workers: int, *,
                 calendar: list[str] | None = None, as_of: str | None = None,
                 daily: bool = False):
    securities = state.securities[(batch - 1) * 100: batch * 100]
    calendar = calendar or state.calendar
    as_of = as_of or state.checkpoint["as_of"]
    rows, statuses, reasons, splits = [], [], {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_security, sec, calendar, as_of, daily)
                   for sec in securities]
        for security, future in zip(securities, futures):
            item_rows, flags, events, reason = future.result()
            sid = security["security_id"]
            rows.extend(item_rows)
            splits.extend(events)
            statuses.append({"security_id": sid, "status": flags})
            if reason:
                reasons[sid] = reason
    if len(statuses) != len(securities) or len({s["security_id"] for s in statuses}) != len(securities):
        raise RuntimeError("BATCH_STATUS_COVERAGE_FAILED")
    return rows, statuses, reasons, splits


def commit_batch(drive: Drive, state: MarketState, batch: int, workers: int):
    rows, statuses, reasons, splits = batch_result(state, batch, workers)
    cp = state.checkpoint
    market = state.name
    base = lines_gz(rows)
    stem = f"{market}/BASE/batch-{batch:04d}"
    status_path = f"{market}/STATUS/batch-{batch:04d}.status.json"
    sec_ids = [s["security_id"] for s in statuses]
    manifest = compact({
        "market": market, "as_of": cp["as_of"], "security_ids": sec_ids,
        "security_count": len(sec_ids), "row_count": len(rows), "sha256": digest(base),
        "statuses": {s["security_id"]: s["status"] for s in statuses},
        "failure_reasons": reasons, "split_events": splits, "worker_id": "hunter-global-actions-v1",
    })
    status_bytes = compact(statuses)
    # A verified receipt is the commit marker. Incomplete writes are retried;
    # existing verified bytes are never replaced.
    drive.put(stem + ".ndjson.gz", base, "application/x-gzip")
    drive.put(stem + ".manifest.json", manifest)
    drive.put(status_path, status_bytes)
    if len(parse_lines_gz(drive.read(stem + ".ndjson.gz"))) != len(rows):
        raise RuntimeError("ROW_COUNT_READBACK_MISMATCH")
    receipt = {
        "market": market, "batch": batch, "sha256": digest(base), "bytes": len(base),
        "manifest_sha256": digest(manifest), "status_sha256": digest(status_bytes),
        "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    drive.append_repairs(market, batch, statuses, reasons)
    drive.put(f"{market}/VERIFIED/batch-{batch:04d}.json", compact(receipt), immutable=True)
    cp.setdefault("verified_batches", {})[str(batch)] = {
        "sha256": digest(base), "row_count": len(rows),
        "security_count": len(statuses), "worker_id": "hunter-global-actions-v1",
    }
    cp["next_batch"] = batch + 1
    cp["partial_batch"] = None
    cp["partial_security_count"] = 0
    cp["updated_at_myt"] = now_myt()
    drive.put(f"{market}/CHECKPOINT.json", compact(cp))
    LOG.info("verified market=%s batch=%04d securities=%d rows=%d", market, batch, len(statuses), len(rows))


def run_base(drive: Drive, workers: int, markets: tuple[str, ...] = MARKETS):
    states = {market: load_market(drive, market) for market in markets}
    pending = {}
    for market, state in states.items():
        LOG.info("resume market=%s target=%d verified_checkpoint=%d total_batches=%d",
                 market, state.checkpoint["target"], len(state.checkpoint.get("verified_batches", {})),
                 state.checkpoint["total_batches"])
        cp = state.checkpoint
        receipts = {item["name"] for item in drive.list(drive.folder(market + "/VERIFIED"))}
        pending[market] = []
        for batch in range(1, cp["total_batches"] + 1):
            filename = f"batch-{batch:04d}.json"
            if filename in receipts:
                if str(batch) not in cp.get("verified_batches", {}):
                    receipt = verified_receipt(drive, market, batch)
                    if not verified_integrity(drive, market, batch, receipt):
                        raise RuntimeError(f"VERIFIED_CORRUPT:{market}:{batch}")
                    info = drive.json(f"{market}/BASE/batch-{batch:04d}.manifest.json")
                    cp.setdefault("verified_batches", {})[str(batch)] = {
                        "sha256": receipt["sha256"], "row_count": info["row_count"],
                        "security_count": info["security_count"], "worker_id": "reconciled",
                    }
                    cp["updated_at_myt"] = now_myt()
                    drive.put(f"{market}/CHECKPOINT.json", compact(cp))
            else:
                if str(batch) in cp.get("verified_batches", {}):
                    raise RuntimeError(f"CHECKPOINT_WITHOUT_VERIFIED:{market}:{batch}")
                pending[market].append(batch)
    while any(pending.values()):
        for market, queue in pending.items():
            if queue:
                commit_batch(drive, states[market], queue.pop(0), workers)
    for market, state in states.items():
        LOG.info("base complete market=%s verified=%d total=%d", market,
                 len(state.checkpoint["verified_batches"]), state.checkpoint["total_batches"])


def session_closed(day: dt.date, local_now: dt.datetime) -> bool:
    return day < local_now.date() or (day == local_now.date() and
                                      local_now.time() >= dt.time(17, 30))


def closed_dates_since(market: str, after_date: str) -> list[str]:
    symbol = "^GSPC" if market == "US" else "^HSI"
    zone = ZoneInfo("America/New_York" if market == "US" else "Asia/Hong_Kong")
    local_now = dt.datetime.now(zone)
    today = local_now.date()
    result = yahoo_chart(symbol, after_date, today.isoformat())
    offset = int((result.get("meta") or {}).get("gmtoffset") or (8 * 3600 if market == "HK" else -4 * 3600))
    dates = [dt.datetime.fromtimestamp(ts + offset, dt.timezone.utc).date()
             for ts in result.get("timestamp") or []]
    # Schedulers fire at 17:30 in each market's own timezone. The current
    # session is eligible only after that local time and only if Yahoo has a bar.
    completed = sorted({day.isoformat() for day in dates
                        if after_date < day.isoformat() and session_closed(day, local_now)})
    return completed


def run_daily(drive: Drive, workers: int, markets: tuple[str, ...] = MARKETS):
    states = {market: load_market(drive, market) for market in markets}
    for market, state in states.items():
        if len(state.checkpoint.get("verified_batches", {})) != state.checkpoint["total_batches"]:
            raise RuntimeError("BASE_INCOMPLETE:" + market)
        summary_path = f"{market}/DAILY/CHECKPOINT.json"
        summary = drive.json(summary_path) if drive.file(summary_path) else {
            "market": market, "last_completed_date": state.checkpoint["as_of"]}
        if summary["market"] != market or summary["last_completed_date"] < state.checkpoint["as_of"]:
            raise RuntimeError("DAILY_SUMMARY_INVALID:" + market)
        for as_of in closed_dates_since(market, summary["last_completed_date"]):
            commit_daily_date(drive, state, workers, as_of)
            summary["last_completed_date"] = as_of
            summary["updated_at_myt"] = now_myt()
            drive.put(summary_path, compact(summary))


def commit_daily_date(drive: Drive, state: MarketState, workers: int, as_of: str):
        market = state.name
        prefix = f"{market}/DAILY/{as_of}"
        drive.folder(prefix, create=True)
        log_path = prefix + "/CHECKPOINT.json"
        existing = drive.file(log_path)
        cp = drive.json(log_path) if existing else {
            "market": market, "as_of": as_of, "target": len(state.securities),
            "verified_batches": {}, "total_batches": state.checkpoint["total_batches"],
            "updated_at_myt": now_myt(),
        }
        if cp["as_of"] != as_of or cp["target"] != len(state.securities):
            raise RuntimeError("DAILY_IDENTITY_MISMATCH:" + market)
        for batch in range(1, cp["total_batches"] + 1):
            stem = f"{prefix}/batch-{batch:04d}"
            receipt_path = stem + ".verified.json"
            if drive.file(receipt_path):
                receipt = drive.json(receipt_path)
                if (digest(drive.read(stem + ".ndjson.gz")) != receipt["sha256"] or
                        digest(drive.read(stem + ".manifest.json")) != receipt["manifest_sha256"]):
                    raise RuntimeError("DAILY_VERIFIED_CORRUPT:" + receipt_path)
                if str(batch) not in cp["verified_batches"]:
                    cp["verified_batches"][str(batch)] = receipt
                    drive.put(log_path, compact(cp))
                continue
            rows, statuses, reasons, splits = batch_result(
                state, batch, workers, calendar=[as_of], as_of=as_of, daily=True)
            content = lines_gz(rows)
            manifest = compact({"market": market, "as_of": as_of, "batch": batch,
                                "security_ids": [x["security_id"] for x in statuses],
                                "row_count": len(rows), "security_count": len(statuses),
                                "sha256": digest(content), "statuses": statuses,
                                "failure_reasons": reasons, "split_events": splits})
            drive.put(stem + ".ndjson.gz", content, "application/x-gzip")
            drive.put(stem + ".manifest.json", manifest)
            receipt = {"market": market, "as_of": as_of, "batch": batch,
                       "sha256": digest(content), "manifest_sha256": digest(manifest),
                       "security_count": len(statuses), "row_count": len(rows),
                       "verified_at": now_myt()}
            drive.put(receipt_path, compact(receipt), immutable=True)
            cp["verified_batches"][str(batch)] = receipt
            cp["updated_at_myt"] = now_myt()
            drive.put(log_path, compact(cp))
            LOG.info("daily market=%s date=%s batch=%04d rows=%d", market, as_of, batch, len(rows))


def probe(drive: Drive | None, markets: tuple[str, ...] = MARKETS):
    if drive is None:
        LOG.info("probe mode=source-only; Drive OAuth unavailable")
    else:
        drive.health()
        for market in markets:
            state = load_market(drive, market)
            receipt_count = len(drive.list(drive.folder(market + "/VERIFIED")))
            LOG.info("probe market=%s checkpoint=%d receipts=%d target=%d partial=%s",
                     market, len(state.checkpoint.get("verified_batches", {})), receipt_count,
                     state.checkpoint["target"], state.checkpoint.get("partial_batch"))
    # Read-only source test, including the runner's outbound network access.
    for symbol in (("AAPL",) if markets == ("US",) else
                   ("0001.HK",) if markets == ("HK",) else ("AAPL", "0001.HK")):
        data = yahoo_chart(symbol, "2026-09-01", "2026-09-25")
        LOG.info("probe symbol=%s candles=%d", symbol, len(data.get("timestamp") or []))



def run_mini(drive: Drive, markets: tuple[str, ...]):
    """One real security per market, isolated from production checkpoints."""
    import base64
    for market in markets:
        base_path = f"{market}/BASE/batch-0001.ndjson.gz"
        before = digest(drive.read(base_path))
        probe = b"HUNTER_BRIDGE_WRITE_GUARD_PROBE"
        for protected, expected in ((base_path, "BASE_SEALED"),
                                    (f"{market}/DAILY/2099-01-01/probe.ndjson.gz", "DAILY_APPEND_ONLY")):
            try:
                drive._call("put", path=protected, sha256=digest(probe),
                            data_base64=base64.b64encode(probe).decode("ascii"),
                            mime="application/octet-stream")
            except RuntimeError as exc:
                if "BRIDGE_" + expected not in str(exc):
                    raise
            else:
                raise RuntimeError("PATH_GUARD_FAILED:" + protected)
        if digest(drive.read(base_path)) != before:
            raise RuntimeError("BASE_CHANGED_AFTER_REJECT:" + market)
        state = load_market(drive, market)
        security = state.securities[0]
        rows, flags, splits, reason = fetch_security(
            security, state.calendar, state.checkpoint["as_of"])
        if not rows:
            raise RuntimeError("MINI_FETCH_FAILED:" + market + ":" + str(reason))
        payload = lines_gz(rows)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = f"_BRIDGE_TEST/{market}/mini-{stamp}.ndjson.gz"
        drive.put(path, payload, "application/x-gzip", immutable=True)
        segment = f"_BRIDGE_TEST/DAILY/{market}/mini-{stamp}.ndjson.gz"
        drive.append(segment, payload, "application/x-gzip")
        try:
            drive.append(segment, payload, "application/x-gzip")
        except RuntimeError as exc:
            if "BRIDGE_APPEND_CONFLICT" not in str(exc):
                raise
        else:
            raise RuntimeError("APPEND_GUARD_FAILED:" + market)
        LOG.info("mini market=%s security=%s rows=%d sha256=%s flags=%s split_count=%d",
                 market, security["security_id"], len(rows), digest(payload), flags, len(splits))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("probe", "mini", "base", "daily", "auto",
                                           "repair", "universe", "options", "analytics",
                                           "bootstrap", "calendar"), default="probe")
    parser.add_argument("--market", choices=MARKETS, help="Run one market in an independent job")
    parser.add_argument("--as-of", help="Explicit historical close for calendar/derived")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    authenticated = all(os.getenv(v) for v in ("APPS_SCRIPT_WEBAPP_URL", "APPS_SCRIPT_SHARED_KEY"))
    drive = Drive() if authenticated else None
    markets = (args.market,) if args.market else MARKETS
    if args.mode == "probe":
        probe(drive, markets)
        return
    if not authenticated:
        raise RuntimeError("BRIDGE_AUTH_MISSING")
    if args.mode == "mini":
        run_mini(drive, markets)
        return
    if os.getenv("HUNTER_ACTIONS_CUTOVER") != "CONFIRMED":
        raise RuntimeError("SINGLE_WRITER_NOT_CONFIRMED: disable Apps Script triggers first")
    workers = max(1, min(10, int(os.getenv("FETCH_WORKERS", "6"))))
    if args.mode == "base":
        raise RuntimeError("BASE_SEALED")
    if args.mode == "repair":
        from repair import run_repair
        for market in markets:
            LOG.info("repair result=%s", run_repair(drive, market))
        return
    if args.mode == "bootstrap":
        from foundation import current_universe, initialize_control, seed_corporate_actions
        from market_calendar import materialize
        for market in markets:
            current_universe(drive, market)
            checkpoint = initialize_control(drive, market)
            LOG.info("bootstrap market=%s splits=%d calendar=%d", market,
                     seed_corporate_actions(drive, market),
                     materialize(drive, market, args.as_of or checkpoint["last_completed_date"]))
        return
    if args.mode == "calendar":
        from market_calendar import materialize
        for market in markets:
            date = args.as_of or drive.json(f"{market}/CONTROL/DAILY_CHECKPOINT.json")["last_completed_date"]
            LOG.info("calendar market=%s rows=%d", market, materialize(drive, market, date))
        return
    if args.mode == "universe":
        from universe import refresh
        for market in markets:
            LOG.info("universe result=%s", refresh(drive, market))
        return
    if args.mode == "options":
        from options import monthly
        for market in markets:
            LOG.info("options market=%s rows=%d", market, monthly(drive, market))
        return
    if args.mode == "analytics":
        from derived import build
        for market in markets:
            path = f"{market}/CONTROL/DAILY_CHECKPOINT.json"
            date = args.as_of or drive.json(path)["last_completed_date"]
            LOG.info("derived market=%s date=%s rows=%d", market, date, build(drive, market, date))
        return
    from foundation import run_daily as run_foundation_daily, seed_corporate_actions
    for market in markets:
        seed_corporate_actions(drive, market)
        result = run_foundation_daily(drive, market, workers)
        LOG.info("daily market=%s sessions=%d written=%d", market, len(result),
                 sum(item.get("written", 0) for item in result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOG.exception("hunter halted: %s", exc)
        sys.exit(1)
