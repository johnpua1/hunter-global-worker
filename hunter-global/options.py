"""Monthly option availability labels, never a vertical trade decision."""
from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

from runner import Drive, compact, now_myt, retry_http, TZ
from foundation import current_universe


def label(symbol: str, market: str) -> dict:
    checked = now_myt()
    unknown = {"has_options": "UNKNOWN", "expiry_available": "UNKNOWN",
               "nearest_expiry": None, "expiry_count": 0,
               "strike_data_available": "UNKNOWN", "bid_ask_available": "UNKNOWN",
               "vertical_usable": "UNKNOWN", "checked_at_myt": checked,
               "source": "Yahoo optionChain", "status": "SOURCE_UNAVAILABLE"}
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/options/" + quote(symbol, safe="")
        result = retry_http(requests.Session(), "GET", url,
                            headers={"User-Agent": "Mozilla/5.0 Hunter/1.0"}).json()
        option = (result.get("optionChain") or {}).get("result")
        if not isinstance(option, list) or not option:
            return unknown
        expirations = option[0].get("expirationDates")
        if not isinstance(expirations, list):
            return unknown
        if not expirations:
            return {**unknown, "has_options": "FALSE", "expiry_available": "FALSE",
                    "status": "NO_EXPIRIES"}
        chains = (option[0].get("options") or [])
        contracts = [contract for chain in chains for kind in ("calls", "puts")
                     for contract in (chain.get(kind) or [])]
        strikes = len({c.get("strike") for c in contracts if c.get("strike") is not None}) >= 2
        quotes = any(c.get("bid") is not None and c.get("ask") is not None and
                     c["ask"] >= c["bid"] >= 0 for c in contracts)
        # A single option quote does not prove two executable legs at one expiry.
        vertical_evidence = None
        for chain in chains:
            for kind in ("calls", "puts"):
                quoted = [c for c in (chain.get(kind) or [])
                          if c.get("bid") is not None and c.get("ask") is not None and
                          c["ask"] >= c["bid"] >= 0 and c.get("strike") is not None]
                pairs = [(a, b) for i, a in enumerate(quoted)
                         for b in quoted[i + 1:] if a["strike"] != b["strike"]]
                if pairs and chain.get("expirationDate") in expirations:
                    a, b = pairs[0]
                    vertical_evidence = {"expiry": dt.datetime.fromtimestamp(
                        chain["expirationDate"], dt.timezone.utc).date().isoformat(),
                        "side": kind, "legs": [{k: c[k] for k in ("strike", "bid", "ask")}
                                                for c in (a, b)]}
                    break
            if vertical_evidence:
                break
        return {**unknown, "has_options": "TRUE", "expiry_available": "TRUE",
                "nearest_expiry": dt.datetime.fromtimestamp(min(expirations), dt.timezone.utc)
                .date().isoformat(), "expiry_count": len(expirations),
                "strike_data_available": "TRUE" if strikes else "UNKNOWN",
                "bid_ask_available": "TRUE" if quotes else "UNKNOWN",
                "vertical_usable": "TRUE" if vertical_evidence else "UNKNOWN",
                "vertical_evidence": vertical_evidence, "status": "CHECKED"}
    except Exception:
        return unknown


def monthly(drive: Drive, market: str):
    month = dt.datetime.now(TZ).strftime("%Y-%m")
    path = f"{market}/OPTIONS_METADATA/{month}.json"
    if drive.file(path):
        return 0
    securities = current_universe(drive, market)
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = pool.map(lambda s: label(s["ticker"], market), securities)
        values = [{"security_id": s["security_id"], **status}
                  for s, status in zip(securities, statuses)]
    drive.put(path, compact({"market": market, "month": month, "items": values}), immutable=True)
    return len(values)


def current_status(item: dict, month: str) -> str:
    checked = item["checked_at_myt"][:7]
    current = dt.date.fromisoformat(month + "-01")
    past = dt.date.fromisoformat(checked + "-01")
    return "STALE" if (current.year - past.year) * 12 + current.month - past.month >= 1 else item["status"]
