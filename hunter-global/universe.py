"""Weekly official listing refresh. Identity ambiguity stays in review."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections import defaultdict

import openpyxl
import requests

from runner import Drive, compact, digest, now_myt, retry_http
from foundation import current_universe, append_queue

US_URLS = {
    "nasdaqlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "otherlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}
HK_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"


def source_bytes(market):
    urls = US_URLS if market == "US" else {"ListOfSecurities.xlsx": HK_URL}
    return {name: retry_http(requests.Session(), "GET", url,
                             headers={"User-Agent": "Mozilla/5.0 Hunter/1.0"}).content
            for name, url in urls.items()}


def parse_us(sources):
    result = {}
    for filename, raw in sources.items():
        lines = raw.decode("utf-8-sig").splitlines()
        if not any(line.startswith("File Creation Time:") for line in lines[-3:]):
            raise RuntimeError("OFFICIAL_US_TIMESTAMP_MISSING")
        for row in csv.DictReader((s for s in lines if "|" in s and not s.startswith("File Creation Time:")),
                                  delimiter="|"):
            ticker = row.get("Symbol") or row.get("ACT Symbol")
            name = row.get("Security Name", "")
            if (not ticker or row.get("ETF") != "N" or row.get("Test Issue") != "N"
                    or any(word in name.upper() for word in
                           ("WARRANT", "PREFERRED", "PREF ", " PFD", "RIGHT", " UNIT", " REIT", " FUND",
                            "SENIOR NOTES", "SUBORDINATED NOTES", "NOTES DUE", "DEBENTURE",
                            "MORTGAGE BOND", "TRUST PREFERRED"))):
                continue
            exchange = "NASDAQ" if filename.startswith("nasdaq") else {
                "N": "NYSE", "A": "NYSE_AMERICAN", "P": "NYSE_ARCA"}.get(row.get("Exchange"))
            if exchange not in ("NASDAQ", "NYSE", "NYSE_AMERICAN"):
                continue
            result[ticker] = {"ticker": ticker.replace(".", "-"), "exchange": exchange,
                              "name": name, "source_symbol": ticker}
    if len(result) < 1000:
        raise RuntimeError("OFFICIAL_US_LIST_INCOMPLETE")
    return result


def parse_hk(sources):
    workbook = openpyxl.load_workbook(io.BytesIO(sources["ListOfSecurities.xlsx"]),
                                      read_only=True, data_only=True)
    sheet = workbook.active
    # HKEX occasionally publishes a valid workbook with a stale worksheet
    # dimension (for example A1:R8 while the sheet contains thousands of rows).
    # openpyxl read_only trusts that metadata unless dimensions are reset.
    sheet.reset_dimensions()
    rows = sheet.iter_rows(values_only=True)
    next(rows); stamp = next(rows)
    if "Updated as at" not in str(stamp[0]):
        raise RuntimeError("OFFICIAL_HK_TIMESTAMP_MISSING")
    headings = [str(x) if x else "" for x in next(rows)]
    idx = {key: headings.index(key) for key in
           ("Stock Code", "Name of Securities", "Category", "Sub-Category", "ISIN",
            "Trading Currency")}
    result = {}
    for row in rows:
        category = str(row[idx["Category"]] or "").strip()
        sub_category = str(row[idx["Sub-Category"]] or "").strip().lower()
        trading_currency = str(row[idx["Trading Currency"]] or "").strip().upper()
        name = str(row[idx["Name of Securities"]] or "").strip()
        # V3 authority: HK Main Board/GEM company shares in HKD. This excludes
        # investment companies, RMB counters, USD preference shares and other
        # non-common-equity rows even when HKEX labels them under Equity.
        if category.lower() != "equity":
            continue
        if "main board" not in sub_category and "gem" not in sub_category:
            continue
        if trading_currency != "HKD":
            continue
        if "PREF" in name.upper():
            continue
        code = str(row[idx["Stock Code"]]).zfill(5)
        result[code] = {"ticker": f"{int(code):04d}.HK", "exchange": "HKEX",
                        "name": row[idx["Name of Securities"]], "isin": row[idx["ISIN"]],
                        "source_symbol": code}
    if len(result) < 1000:
        raise RuntimeError("OFFICIAL_HK_LIST_INCOMPLETE")
    return result


def refresh(drive: Drive, market: str):
    path = f"{market}/CURRENT_UNIVERSE.json"
    current_universe(drive, market)
    original = drive.read(path)
    doc = json.loads(original)
    sources = source_bytes(market)
    observed = parse_us(sources) if market == "US" else parse_hk(sources)
    def symbol_key(sec):
        raw = sec.get("source_row") or {}
        return (sec.get("source_symbol") or raw.get("ACT Symbol") or raw.get("Symbol") or
                (sec["ticker"].replace(".HK", "").zfill(5) if market == "HK" else sec["ticker"]))
    listed = {symbol_key(s): s for s in doc["securities"] if s.get("listing_status") == "ACTIVE"}
    # A failed or partial vendor download must never delist thousands of IDs.
    missing = set(listed) - set(observed)
    if len(missing) > max(25, len(listed) * .05):
        raise RuntimeError("OFFICIAL_LIST_SHRINK_GUARD")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for name, raw in sources.items():
        drive.put(f"{market}/SOURCES/weekly-{stamp}-{name}", raw,
                  mime="application/octet-stream", immutable=True)
    max_id = max(int(s["security_id"].rsplit("-", 1)[1]) for s in doc["securities"])
    known = defaultdict(list)
    known_isin = defaultdict(list)
    for sec in doc["securities"]:
        known[symbol_key(sec)].append(sec)
        if market == "HK" and sec.get("isin"):
            known_isin[sec["isin"]].append(sec)
    changed, review = 0, []
    for symbol, entry in observed.items():
        matches = known.get(symbol, [])
        if matches:
            sec = matches[-1]
            if market == "HK" and sec.get("isin") and entry.get("isin") and sec["isin"] != entry["isin"]:
                review.append({"market": market, "security_id": sec["security_id"],
                               "category": "IDENTITY_REVIEW", "problem": "ISIN_CHANGED",
                               "status": "OPEN", "recorded_at_myt": now_myt()})
                continue
            if market == "US" and sec.get("name") and sec["name"].strip().lower() != entry["name"].strip().lower():
                review.append({"market": market, "security_id": sec["security_id"],
                               "category": "IDENTITY_REVIEW", "problem": "NAME_CHANGED",
                               "status": "OPEN", "recorded_at_myt": now_myt()})
                continue
            next_status = ("QUARANTINED_DATA_GAP"
                           if sec.get("listing_status") == "QUARANTINED_DATA_GAP"
                           else "ACTIVE")
            sec.update({**entry, "listing_status": next_status, "identity_review": False,
                        "official_listing_evidence": {
                            "source_hash": digest(sources["ListOfSecurities.xlsx" if market == "HK"
                                                         else "nasdaqlisted.txt" if entry["exchange"] == "NASDAQ"
                                                         else "otherlisted.txt"]),
                            "ticker": entry["ticker"], "exchange": entry["exchange"],
                            "isin": entry.get("isin")}})
        else:
            by_isin = known_isin.get(entry.get("isin"), []) if market == "HK" else []
            if entry.get("isin") and len(by_isin) == 1 and symbol_key(by_isin[0]) in missing:
                sec = by_isin[0]
                old_ticker = sec["ticker"]
                old_key = symbol_key(sec)
                sec.update({**entry, "listing_status": "ACTIVE",
                            "identity_proof": {"kind": "HK_ISIN_MATCH",
                                               "isin": entry["isin"],
                                               "old_ticker": old_ticker,
                                               "new_ticker": entry["ticker"],
                                               "source_hash": digest(sources["ListOfSecurities.xlsx"])}})
                review.append({"market": market, "security_id": sec["security_id"],
                               "category": "IDENTITY_REVIEW", "problem": "OFFICIAL_ISIN_SAME_NEW_TICKER",
                               "status": "OPEN", "recorded_at_myt": now_myt()})
                missing.remove(old_key)
                continue
            max_id += 1
            doc["securities"].append({**entry, "market": market,
                                       "currency": "USD" if market == "US" else "HKD",
                                       "security_id": f"{market}-{max_id:06d}",
                                       "security_id_origin": "NEW_LISTING",
                                       "listing_status": "ACTIVE", "history_status": "SHORT_HISTORY"})
            changed += 1
    # Absence from a weekly list alone is not a verified delisting.
    for symbol in missing:
        sec = listed[symbol]
        sec["review_status"] = "LISTING_STATUS_REVIEW"
        review.append({"market": market, "security_id": sec["security_id"],
                       "category": "IDENTITY_REVIEW", "problem": "ABSENT_OFFICIAL_SNAPSHOT",
                       "status": "OPEN", "recorded_at_myt": now_myt()})
    append_queue(drive, review)
    doc["updated_at_myt"] = now_myt()
    doc["source_hashes"] = {name: digest(raw) for name, raw in sources.items()}
    drive.put(path, compact(doc), expected_sha=digest(original))
    return {"market": market, "new": changed, "review": len(review),
            "total": len(doc["securities"])}
