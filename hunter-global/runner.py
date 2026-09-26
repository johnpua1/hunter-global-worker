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
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials


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
    for attempt in range(5):
        try:
            response = session.request(method, url, timeout=45, **kwargs)
            if response.status_code not in (429, 500, 502, 503, 504):
                response.raise_for_status()
                return response
            raise requests.HTTPError(f"HTTP {response.status_code}")
        except (requests.RequestException, OSError):
            if attempt == 4:
                raise
            time.sleep(min(30, 2**attempt + random.random()))
    raise AssertionError("unreachable")


class Drive:
    def __init__(self, root_id: str):
        needed = (
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_OAUTH_REFRESH_TOKEN",
        )
        missing = [key for key in needed if not os.environ.get(key)]
        if missing:
            raise RuntimeError("DRIVE_AUTH_MISSING:" + ",".join(missing))
        credentials = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        self.http = AuthorizedSession(credentials)
        self.root_id = root_id
        self.folders: dict[str, str] = {"": root_id}

    def list(self, parent_id: str, name: str | None = None) -> list[dict]:
        query = f"'{parent_id}' in parents and trashed = false"
        if name is not None:
            query += " and name = '" + name.replace("\\", "\\\\").replace("'", "\\'") + "'"
        results, token = [], None
        while True:
            params = {
                "q": query,
                "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum)",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            response = retry_http(self.http, "GET", ROOT, params=params).json()
            results.extend(response.get("files", []))
            token = response.get("nextPageToken")
            if not token:
                return results

    def folder(self, path: str, create: bool = False) -> str:
        path = path.strip("/")
        if path in self.folders:
            return self.folders[path]
        parent, _, name = path.rpartition("/")
        parent_id = self.folder(parent, create=create)
        matches = self.list(parent_id, name)
        if len(matches) > 1:
            raise RuntimeError("DUPLICATE_FOLDER:" + path)
        if not matches:
            if not create:
                raise FileNotFoundError(path)
            metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
            item = retry_http(self.http, "POST", ROOT, json=metadata, params={"fields": "id"}).json()
        else:
            item = matches[0]
            if item["mimeType"] != "application/vnd.google-apps.folder":
                raise RuntimeError("NOT_FOLDER:" + path)
        self.folders[path] = item["id"]
        return item["id"]

    def file(self, path: str) -> dict | None:
        directory, _, name = path.strip("/").rpartition("/")
        parent = self.folder(directory)
        matches = self.list(parent, name)
        if len(matches) > 1:
            raise RuntimeError("DUPLICATE_FILE:" + path)
        return matches[0] if matches else None

    def read(self, path: str) -> bytes:
        item = self.file(path)
        if not item:
            raise FileNotFoundError(path)
        return retry_http(self.http, "GET", ROOT + "/" + item["id"], params={"alt": "media"}).content

    def json(self, path: str) -> Any:
        return json.loads(self.read(path))

    def put(self, path: str, content: bytes, mime: str = "application/json", immutable=False):
        directory, _, name = path.strip("/").rpartition("/")
        parent = self.folder(directory, create=True)
        existing = self.file(path)
        if existing and immutable:
            if digest(self.read(path)) == digest(content):
                return existing
            raise RuntimeError("IMMUTABLE_CONFLICT:" + path)
        if existing:
            response = retry_http(
                self.http, "PATCH", UPLOAD + "/" + existing["id"],
                params={"uploadType": "media", "fields": "id,name,size"},
                data=content, headers={"Content-Type": mime},
            )
        else:
            boundary = "hunter-" + hashlib.sha256(content).hexdigest()[:16]
            metadata = compact({"name": name, "parents": [parent]})
            body = (
                b"--" + boundary.encode() + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + metadata + b"\r\n--" + boundary.encode() + b"\r\nContent-Type: "
                + mime.encode() + b"\r\n\r\n" + content + b"\r\n--" + boundary.encode() + b"--\r\n"
            )
            # A timed-out create may already have succeeded. Resolve by name
            # before a retry so an ambiguous response cannot duplicate a file.
            for attempt in range(3):
                try:
                    response = self.http.post(
                        UPLOAD, params={"uploadType": "multipart", "fields": "id,name,size"},
                        data=body, headers={"Content-Type": "multipart/related; boundary=" + boundary},
                        timeout=90,
                    )
                    response.raise_for_status()
                    break
                except requests.RequestException:
                    found = self.file(path)
                    if found:
                        if digest(self.read(path)) != digest(content):
                            raise RuntimeError("CREATE_AMBIGUOUS_CONFLICT:" + path)
                        return found
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
        if digest(self.read(path)) != digest(content):
            raise RuntimeError("DRIVE_READBACK_MISMATCH:" + path)
        return response.json()


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
        if any(r["volume"] == 0 and len({r[k] for k in ("open", "high", "low", "close")}) == 1 for r in rows):
            if "DATA_SUSPECT" not in flags:
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


def closed_dates_since(market: str, after_date: str) -> list[str]:
    symbol = "^GSPC" if market == "US" else "^HSI"
    today = dt.datetime.now(TZ).date()
    result = yahoo_chart(symbol, after_date, today.isoformat())
    offset = int((result.get("meta") or {}).get("gmtoffset") or (8 * 3600 if market == "HK" else -4 * 3600))
    dates = [dt.datetime.fromtimestamp(ts + offset, dt.timezone.utc).date()
             for ts in result.get("timestamp") or []]
    # At 08:37 MYT both markets' prior trading sessions have closed. Keep
    # every missed session since the last fully committed daily checkpoint.
    completed = sorted({day.isoformat() for day in dates
                        if after_date < day.isoformat() and day < today})
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("probe", "base", "daily"), default="probe")
    parser.add_argument("--market", choices=MARKETS, help="Run one market in an independent job")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root_id = os.getenv("HUNTER_GLOBAL_FOLDER_ID")
    if not root_id and args.mode != "probe":
        raise RuntimeError("HUNTER_GLOBAL_FOLDER_ID_MISSING")
    authenticated = all(os.getenv(v) for v in
                        ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"))
    drive = Drive(root_id) if authenticated and root_id else None
    markets = (args.market,) if args.market else MARKETS
    if args.mode == "probe":
        probe(drive, markets)
        return
    if not authenticated:
        raise RuntimeError("DRIVE_AUTH_MISSING: one-time Google offline OAuth required")
    if os.getenv("HUNTER_SINGLE_WRITER_CUTOVER") != "CONFIRMED":
        raise RuntimeError("SINGLE_WRITER_NOT_CONFIRMED: disable Apps Script triggers first")
    workers = max(1, min(10, int(os.getenv("FETCH_WORKERS", "6"))))
    if args.mode == "base":
        run_base(drive, workers, markets)
    else:
        run_daily(drive, workers, markets)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOG.exception("hunter halted: %s", exc)
        sys.exit(1)
