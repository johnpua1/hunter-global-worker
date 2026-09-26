"""Monthly option availability labels, never a vertical trade decision."""
from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

from runner import Drive, compact, now_myt, retry_http, TZ
from foundation import current_universe


def label(symbol: str, market: str) -> str:
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/options/" + quote(symbol, safe="")
        result = retry_http(requests.Session(), "GET", url,
                            headers={"User-Agent": "Mozilla/5.0 Hunter/1.0"}).json()
        option = (result.get("optionChain") or {}).get("result")
        if not isinstance(option, list) or not option:
            return "UNKNOWN"
        expirations = option[0].get("expirationDates")
        if not isinstance(expirations, list):
            return "UNKNOWN"
        if market == "HK" and not expirations:
            return "UNKNOWN"
        return "TRUE" if expirations else "FALSE"
    except Exception:
        return "UNKNOWN"


def monthly(drive: Drive, market: str):
    month = dt.datetime.now(TZ).strftime("%Y-%m")
    path = f"{market}/OPTIONS_METADATA/{month}.json"
    if drive.file(path):
        return 0
    securities = current_universe(drive, market)
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = pool.map(lambda s: label(s["ticker"], market), securities)
        values = [{"security_id": s["security_id"], "status": status,
                   "checked_at_myt": now_myt(), "vertical_usable": "UNKNOWN",
                   "vertical_requires": ["expiry", "strike", "bid_ask"]}
                  for s, status in zip(securities, statuses)]
    drive.put(path, compact({"market": market, "month": month, "items": values}), immutable=True)
    return len(values)


def current_status(item: dict, month: str) -> str:
    checked = item["checked_at_myt"][:7]
    current = dt.date.fromisoformat(month + "-01")
    past = dt.date.fromisoformat(checked + "-01")
    return "STALE" if (current.year - past.year) * 12 + current.month - past.month >= 1 else item["status"]
