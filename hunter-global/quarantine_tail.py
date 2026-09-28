"""Targeted closeout for the 2026-09-28 Hunter quarantine tail.

Scope is frozen by QUARANTINE_TAIL_TARGET_2026-09-28.json.
BASE/VERIFIED are read-only. Only CURRENT_UNIVERSE, REPAIR_PATCH, REPAIR_QUEUE,
DERIVED and a closeout receipt can change.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
from collections import Counter, defaultdict

from analytics import compose
from derived import build as build_derived, read_files
from foundation import daily_segments
from runner import Drive, compact, digest, fetch_security, load_market, now_myt, parse_lines_gz
from universe import HK_URL, parse_hk, parse_us

TARGET_STATES = {
    "QUARANTINED_UNCONFIRMED_SOURCE",
    "QUARANTINED_IDENTITY",
    "QUARANTINED_DATA_GAP",
}
TARGET_PATH = "US/CONTROL/QUARANTINE_TAIL_TARGET_2026-09-28.json"
RECEIPT_PATH = "US/CONTROL/QUARANTINE_TAIL_CLOSEOUT_2026-09-28.json"
VERSION = "QUARANTINE_TAIL_CLOSEOUT_V1"
EXPECTED = {
    "US": {
        "QUARANTINED_UNCONFIRMED_SOURCE": 274,
        "QUARANTINED_IDENTITY": 141,
        "QUARANTINED_DATA_GAP": 21,
    },
    "HK": {
        "QUARANTINED_UNCONFIRMED_SOURCE": 4,
        "QUARANTINED_IDENTITY": 0,
        "QUARANTINED_DATA_GAP": 5,
    },
}

NON_COMMON_RE = re.compile(
    r"\bWARRANTS?\b|\bPREFERRED\b|\bPREFERENCE\b|\bPREF\b|\bPFD\b|"
    r"\bRIGHTS?\b|\bUNITS?\b|\bREIT\b|\bFUND\b|"
    r"\bSENIOR NOTES\b|\bSUBORDINATED NOTES\b|\bNOTES DUE\b|"
    r"\bDEBENTURES?\b|\bMORTGAGE BONDS?\b|\bTRUST PREFERRED\b",
    re.I,
)
SPAC_RE = re.compile(r"\bSPAC\b", re.I)
ACQ_RE = re.compile(r"\bACQUISITION\b", re.I)
SHARE_RE = re.compile(r"\b(CLASS A|ORDINARY SHARES?|COMMON STOCK)\b", re.I)


def normalized_exchange(value: str | None) -> str | None:
    if not value:
        return value
    return value.upper().replace(" ", "_").replace("-", "_")


def is_spac(name: str) -> bool:
    return bool(SPAC_RE.search(name) or (ACQ_RE.search(name) and SHARE_RE.search(name)))


def terminal_exclusion(name: str) -> str | None:
    if is_spac(name):
        return "EXCLUDED_SPAC"
    if NON_COMMON_RE.search(name):
        return "EXCLUDED_NON_COMMON"
    return None


def parse_us_raw(sources: dict[str, bytes]) -> dict[str, dict]:
    rows = {}
    for filename, raw in sources.items():
        lines = raw.decode("utf-8-sig").splitlines()
        if not any(line.startswith("File Creation Time:") for line in lines[-3:]):
            raise RuntimeError("OFFICIAL_US_TIMESTAMP_MISSING:" + filename)
        source_hash = digest(raw)
        for row in csv.DictReader(
            (line for line in lines if "|" in line and not line.startswith("File Creation Time:")),
            delimiter="|",
        ):
            symbol = row.get("Symbol") or row.get("ACT Symbol")
            if not symbol:
                continue
            exchange = "NASDAQ" if filename.startswith("nasdaq") else {
                "N": "NYSE", "A": "NYSE_AMERICAN", "P": "NYSE_ARCA"
            }.get(row.get("Exchange"))
            rows[symbol] = {
                "source": filename,
                "source_hash": source_hash,
                "source_row": row,
                "ticker": symbol.replace(".", "-"),
                "name": row.get("Security Name", ""),
                "exchange": exchange,
            }
    return rows


def source_symbol(security: dict) -> str:
    row = security.get("source_row") or {}
    return str(
        security.get("source_symbol")
        or row.get("Symbol")
        or row.get("ACT Symbol")
        or security.get("ticker")
        or ""
    )


def us_official_match(security: dict, raw: dict[str, dict]) -> dict | None:
    entry = raw.get(source_symbol(security))
    if not entry:
        return None
    if entry["ticker"] != security.get("ticker"):
        return None
    if normalized_exchange(entry["exchange"]) != normalized_exchange(security.get("exchange")):
        return None
    if str(entry["name"]).strip() != str(security.get("name") or "").strip():
        return None
    return entry


def structural(row: dict) -> bool:
    try:
        vals = [float(row[k]) for k in ("open", "high", "low", "close")]
        volume = float(row["volume"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        min(vals) > 0
        and volume >= 0
        and float(row["high"]) >= max(float(row["open"]), float(row["close"]), float(row["low"]))
        and float(row["low"]) <= min(float(row["open"]), float(row["close"]))
    )


def load_composed(drive: Drive, market: str, target_ids: set[str]):
    state = load_market(drive, market)
    base_by_sid = defaultdict(list)
    batch_for_sid = {}
    for index, security in enumerate(state.securities):
        sid = security["security_id"]
        if sid in target_ids:
            batch_for_sid[sid] = index // 100 + 1
    for batch in sorted(set(batch_for_sid.values())):
        for row in parse_lines_gz(drive.read(f"{market}/BASE/batch-{batch:04d}.ndjson.gz")):
            if row["security_id"] in target_ids:
                base_by_sid[row["security_id"]].append(row)

    patches = defaultdict(list)
    for path in read_files(drive, market, "REPAIR_PATCH", ".json"):
        payload = drive.json(path)
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            for patch in items:
                sid = patch.get("security_id")
                if sid in target_ids:
                    patches[sid].append(patch)
        elif isinstance(payload, dict) and payload.get("security_id") in target_ids:
            patches[payload["security_id"]].append(payload)

    daily = defaultdict(list)
    for day in daily_segments(drive, market):
        for entry in drive.list(f"{market}/DAILY/{day}"):
            if not entry["name"].endswith(".ndjson.gz"):
                continue
            for row in parse_lines_gz(drive.read(f"{market}/DAILY/{day}/{entry['name']}")):
                if row["security_id"] in target_ids:
                    daily[row["security_id"]].append(row)

    composed = {
        sid: compose(base_by_sid[sid], patches[sid], daily[sid])
        for sid in target_ids
    }
    return state, composed


def all_missing(rows: list[dict], calendar: list[str], *, recent_only=False) -> list[str]:
    if not rows:
        return calendar[-20:] if recent_only else list(calendar)
    present = {r.get("trade_date", r["date"]) for r in rows if structural(r)}
    if recent_only:
        expected = calendar[-20:]
    else:
        dated = sorted(present)
        if not dated:
            return calendar[-20:] if recent_only else list(calendar)
        first, last = dated[0], dated[-1]
        expected = [d for d in calendar if first <= d <= last]
        if calendar[-1] > last:
            expected += [d for d in calendar if d > last]
    return [d for d in expected if d not in present]


def queue_reasons(queue: dict, target_ids: set[str]) -> dict[str, list[str]]:
    out = defaultdict(list)
    for item in queue.get("items", []):
        sid = item.get("security_id")
        if sid in target_ids:
            reason = item.get("reason") or item.get("problem")
            if reason:
                out[sid].append(str(reason))
    return out


def needed_dates(original_status: str, rows: list[dict], calendar: list[str], reasons: list[str]) -> list[str]:
    if not rows:
        return list(calendar)
    if original_status == "QUARANTINED_DATA_GAP":
        missing = set(all_missing(rows, calendar, recent_only=False))
        for reason in reasons:
            for value in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", reason):
                if value in calendar:
                    missing.add(value)
        return sorted(missing)
    return all_missing(rows, calendar, recent_only=True)


def refreshed_rows(security: dict, dates: list[str], as_of: str):
    if not dates:
        return [], None
    rows, flags, splits, reason = fetch_security(security, dates, as_of, daily=True)
    valid = [r for r in rows if structural(r)]
    return valid, reason


def make_patch_item(market: str, sid: str, result: str, accepted: bool,
                    rows: list[dict], evidence: dict) -> dict:
    return {
        "market": market,
        "security_id": sid,
        "result": result,
        "accepted": accepted,
        "rows": rows,
        "evidence": evidence,
    }


def write_bulk_patch(drive: Drive, market: str, items: list[dict]) -> str | None:
    if not items:
        return None
    items = sorted(items, key=lambda x: x["security_id"])
    payload = {
        "market": market,
        "kind": VERSION,
        "as_of": "2026-09-25",
        "items": items,
    }
    body = compact(payload)
    path = f"{market}/REPAIR_PATCH/qt/closeout-{digest(body)[:24]}.json"
    if not drive.file(path):
        drive.append(path, body)
    elif digest(drive.read(path)) != digest(body):
        raise RuntimeError("TAIL_PATCH_CONFLICT:" + path)
    return path


def ensure_target_manifest(drive: Drive, universes: dict[str, dict]) -> dict:
    if drive.file(TARGET_PATH):
        manifest = drive.json(TARGET_PATH)
        if manifest.get("version") != VERSION:
            raise RuntimeError("TAIL_TARGET_VERSION_CONFLICT")
        return manifest
    targets = []
    observed = {}
    for market, doc in universes.items():
        counts = Counter(
            s.get("listing_status") for s in doc["securities"]
            if s.get("listing_status") in TARGET_STATES
        )
        observed[market] = {key: counts.get(key, 0) for key in EXPECTED[market]}
        if observed[market] != EXPECTED[market]:
            raise RuntimeError(
                "TAIL_BASELINE_COUNT_MISMATCH:"
                + market + ":" + json.dumps(observed[market], sort_keys=True)
            )
        for security in doc["securities"]:
            status = security.get("listing_status")
            if status in TARGET_STATES:
                targets.append({
                    "market": market,
                    "security_id": security["security_id"],
                    "ticker": security["ticker"],
                    "original_status": status,
                })
    if len(targets) != 445:
        raise RuntimeError("TAIL_TARGET_TOTAL_MISMATCH:" + str(len(targets)))
    manifest = {
        "version": VERSION,
        "created_at_myt": now_myt(),
        "expected": EXPECTED,
        "target_count": 445,
        "targets": targets,
    }
    drive.put(TARGET_PATH, compact(manifest), immutable=True)
    return manifest


def apply_board_lot(hk_doc: dict, hk_source: bytes) -> tuple[int, dict]:
    source_hash = digest(hk_source)
    parsed = parse_hk({"ListOfSecurities.xlsx": hk_source})
    by_code = {str(k).zfill(5): v for k, v in parsed.items()}
    matched = 0
    for security in hk_doc["securities"]:
        code = str(security.get("source_symbol") or security.get("stock_code") or "").zfill(5)
        row = by_code.get(code)
        if not row:
            raise RuntimeError("HK_BOARD_LOT_CODE_MISSING:" + code)
        if str(row.get("name") or "").strip() != str(security.get("name") or "").strip():
            raise RuntimeError("HK_BOARD_LOT_NAME_MISMATCH:" + code)
        old_isin = str(security.get("isin") or "").strip()
        new_isin = str(row.get("isin") or "").strip()
        if old_isin and new_isin and old_isin != new_isin:
            raise RuntimeError("HK_BOARD_LOT_ISIN_MISMATCH:" + code)
        security["board_lot_size"] = int(row["board_lot_size"])
        security["board_lot_source"] = HK_URL
        security["board_lot_as_of"] = row["board_lot_as_of"]
        matched += 1
    if matched != len(hk_doc["securities"]):
        raise RuntimeError("HK_BOARD_LOT_COVERAGE_MISMATCH")
    return matched, {
        "source_sha256": source_hash,
        "as_of": next(iter(parsed.values()))["board_lot_as_of"],
        "coverage": matched,
    }


def process_market(drive: Drive, market: str, doc: dict, manifest_targets: list[dict],
                   queue: dict, us_raw: dict[str, dict] | None):
    by_sid = {s["security_id"]: s for s in doc["securities"]}
    target_ids = {t["security_id"] for t in manifest_targets}
    target_original = {t["security_id"]: t["original_status"] for t in manifest_targets}
    state, composed = load_composed(drive, market, target_ids)
    reasons = queue_reasons(queue, target_ids)

    plans = {}
    for sid in sorted(target_ids):
        security = by_sid.get(sid)
        if not security:
            plans[sid] = {"terminal": "QUARANTINED_IDENTITY",
                          "reason": "SECURITY_ID_MISSING_FROM_CURRENT_UNIVERSE",
                          "fresh_dates": []}
            continue
        if security.get("tail_closeout_version") == VERSION and security.get("listing_status") not in TARGET_STATES:
            plans[sid] = {"terminal": security["listing_status"],
                          "reason": security.get("tail_closeout_reason", "PREVIOUSLY_CLOSED"),
                          "fresh_dates": []}
            continue

        original = target_original[sid]
        official_ok = True
        official_entry = None
        if market == "US":
            official_entry = us_official_match(security, us_raw or {})
            official_ok = official_entry is not None
            exclusion = terminal_exclusion(security.get("name", ""))
            if exclusion and official_ok:
                plans[sid] = {"terminal": exclusion,
                              "reason": "OFFICIAL_LISTING_SECURITY_TYPE_EXCLUDED",
                              "fresh_dates": [], "official": official_entry}
                continue
        else:
            evidence = security.get("official_listing_evidence") or {}
            official_ok = (
                evidence.get("source_hash") == doc.get("source_hashes", {}).get("ListOfSecurities.xlsx")
                and evidence.get("ticker") == security.get("ticker")
                and normalized_exchange(evidence.get("exchange")) == "HKEX"
            )
        if not official_ok:
            plans[sid] = {"terminal": "QUARANTINED_IDENTITY",
                          "reason": "OFFICIAL_LISTING_NOT_CONFIRMED",
                          "fresh_dates": []}
            continue

        rows = composed.get(sid, [])
        dates = needed_dates(original, rows, state.calendar, reasons.get(sid, []))
        if not dates and rows and all(structural(r) for r in rows):
            plans[sid] = {"terminal": "ACTIVE",
                          "reason": "OFFICIAL_LISTING_AND_COMPOSED_HISTORY_CONFIRMED",
                          "fresh_dates": [], "official": official_entry}
        else:
            plans[sid] = {"terminal": None,
                          "reason": "PRIMARY_REFRESH_REQUIRED",
                          "fresh_dates": dates or state.calendar[-20:],
                          "official": official_entry}

    refresh_jobs = [(sid, plan) for sid, plan in plans.items() if plan["terminal"] is None]
    fetched = {}

    def do_refresh(entry):
        sid, plan = entry
        security = by_sid[sid]
        rows, error = refreshed_rows(security, plan["fresh_dates"], state.checkpoint["as_of"])
        return sid, rows, error

    workers = min(8, max(1, len(refresh_jobs)))
    if refresh_jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for sid, rows, error in pool.map(do_refresh, refresh_jobs):
                fetched[sid] = (rows, error)

    patch_items = []
    result_counts = Counter()
    for sid in sorted(target_ids):
        security = by_sid.get(sid)
        plan = plans[sid]
        if security is None:
            result_counts["QUARANTINED_IDENTITY"] += 1
            continue

        terminal = plan["terminal"]
        reason = plan["reason"]
        added_rows = []
        if terminal is None:
            fresh, error = fetched.get(sid, ([], "NO_REFRESH_RESULT"))
            existing = {r.get("trade_date", r["date"]): r for r in composed.get(sid, []) if structural(r)}
            for row in fresh:
                day = row.get("trade_date", row["date"])
                if day not in existing:
                    added_rows.append(row)
                    existing[day] = row
            combined = sorted(existing.values(), key=lambda r: r.get("trade_date", r["date"]))
            remaining = needed_dates(
                target_original[sid], combined, state.calendar, reasons.get(sid, [])
            )
            if combined and not remaining and all(structural(r) for r in combined):
                terminal = "ACTIVE"
                reason = "OFFICIAL_LISTING_AND_PRIMARY_HISTORY_REFRESH_CONFIRMED"
            else:
                original = target_original[sid]
                terminal = ("QUARANTINED_DATA_GAP"
                            if original == "QUARANTINED_DATA_GAP"
                            else "QUARANTINED_UNCONFIRMED_SOURCE")
                reason = (
                    "PRIMARY_REFRESH_INCOMPLETE:"
                    + (error or "MISSING_SESSIONS:" + ",".join(remaining[:12]))
                )

        official = plan.get("official")
        if market == "US" and official:
            security["source"] = official["source"]
            security["source_row"] = official["source_row"]
            security["source_symbol"] = source_symbol(security)
            security["identity_review"] = False
            security["official_listing_evidence"] = {
                "source_hash": official["source_hash"],
                "ticker": official["ticker"],
                "exchange": official["exchange"],
                "isin": None,
            }

        security["listing_status"] = terminal
        security["tail_closeout_version"] = VERSION
        security["tail_closeout_reason"] = reason
        security["tail_closeout_at_myt"] = now_myt()
        if terminal == "ACTIVE":
            security.pop("quarantine_reason", None)
            security["review_status"] = "READY"
        elif terminal.startswith("EXCLUDED_"):
            security["exclusion_reason"] = reason
            security["review_status"] = "EXCLUDED"
        else:
            security["quarantine_reason"] = reason
            security["review_status"] = "QUARANTINED"

        accepted = terminal == "ACTIVE" or terminal.startswith("EXCLUDED_")
        patch_items.append(make_patch_item(
            market, sid, "RESOLVED" if terminal == "ACTIVE" else
            "IDENTITY_FIXED" if terminal.startswith("EXCLUDED_") else terminal,
            accepted, added_rows,
            {
                "version": VERSION,
                "terminal_status": terminal,
                "reason": reason,
                "official_source_confirmed": bool(
                    (security.get("official_listing_evidence") or {}).get("source_hash")
                ),
                "base_immutable": True,
            }
        ))
        result_counts[terminal] += 1

    return result_counts, patch_items


def reconcile_queue(queue: dict, target_ids: set[str], terminal_by_sid: dict[str, dict]) -> int:
    changed = 0
    for item in queue.get("items", []):
        sid = item.get("security_id")
        if sid not in target_ids:
            continue
        if (item.get("status") not in TARGET_STATES
                and item.get("category") != "FINAL_READINESS_TARGET"):
            continue
        terminal = terminal_by_sid[sid]["listing_status"]
        status = "RESOLVED" if terminal == "ACTIVE" else terminal
        reason = terminal_by_sid[sid].get("tail_closeout_reason")
        if item.get("status") != status or item.get("reason") != reason:
            item["status"] = status
            item["reason"] = reason
            item["tail_closeout_version"] = VERSION
            item["verified_at_myt"] = now_myt()
            changed += 1
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default="2026-09-25")
    args = parser.parse_args()

    drive = Drive()
    drive.health()
    base_complete_before = drive.read("BASE_COMPLETE.json")
    base_complete_sha = digest(base_complete_before)
    checkpoint_before = {
        market: digest(drive.read(f"{market}/CHECKPOINT.json"))
        for market in ("US", "HK")
    }

    raw_docs = {market: drive.read(f"{market}/CURRENT_UNIVERSE.json") for market in ("US", "HK")}
    docs = {market: json.loads(raw_docs[market]) for market in ("US", "HK")}
    manifest = ensure_target_manifest(drive, docs)
    if manifest.get("target_count") != 445:
        raise RuntimeError("TAIL_MANIFEST_TARGET_COUNT_INVALID")

    us_sources = {
        name: drive.read("US/SOURCES/" + name)
        for name in ("nasdaqlisted.txt", "otherlisted.txt")
    }
    for name, raw in us_sources.items():
        expected = docs["US"].get("source_hashes", {}).get(name)
        if expected and digest(raw) != expected:
            raise RuntimeError("US_SOURCE_HASH_DRIFT:" + name)
    eligible_us = parse_us(us_sources)
    if len(eligible_us) < 1000:
        raise RuntimeError("US_OFFICIAL_COMMON_UNIVERSE_TOO_SMALL")
    us_raw = parse_us_raw(us_sources)

    hk_source = drive.read("HK/SOURCES/ListOfSecurities.xlsx")
    expected_hk_hash = docs["HK"].get("source_hashes", {}).get("ListOfSecurities.xlsx")
    if expected_hk_hash and digest(hk_source) != expected_hk_hash:
        raise RuntimeError("HK_SOURCE_HASH_DRIFT")
    board_lot_count, board_lot_evidence = apply_board_lot(docs["HK"], hk_source)

    queue_raw = drive.read("REPAIR_QUEUE.json")
    queue = json.loads(queue_raw)

    by_market_targets = {
        market: [t for t in manifest["targets"] if t["market"] == market]
        for market in ("US", "HK")
    }
    results = {}
    patch_paths = {}
    for market in ("US", "HK"):
        counts, patches = process_market(
            drive, market, docs[market], by_market_targets[market], queue,
            us_raw if market == "US" else None,
        )
        results[market] = dict(counts)
        patch_paths[market] = write_bulk_patch(drive, market, patches)
        docs[market]["updated_at_myt"] = now_myt()
        docs[market]["quarantine_tail_closeout_version"] = VERSION

    target_ids = {t["security_id"] for t in manifest["targets"]}
    terminals = {
        s["security_id"]: s
        for market in ("US", "HK")
        for s in docs[market]["securities"]
        if s["security_id"] in target_ids
    }
    queue_changed = reconcile_queue(queue, target_ids, terminals)

    for market in ("US", "HK"):
        drive.put(
            f"{market}/CURRENT_UNIVERSE.json",
            compact(docs[market]),
            expected_sha=digest(raw_docs[market]),
        )
    drive.put("REPAIR_QUEUE.json", compact(queue), expected_sha=digest(queue_raw))

    derived_rows = {
        market: build_derived(drive, market, args.as_of)
        for market in ("US", "HK")
    }

    if digest(drive.read("BASE_COMPLETE.json")) != base_complete_sha:
        raise RuntimeError("BASE_COMPLETE_MUTATED")
    for market in ("US", "HK"):
        if digest(drive.read(f"{market}/CHECKPOINT.json")) != checkpoint_before[market]:
            raise RuntimeError("BASE_CHECKPOINT_MUTATED:" + market)

    final_docs = {market: drive.json(f"{market}/CURRENT_UNIVERSE.json") for market in ("US", "HK")}
    final_counts = {
        market: dict(Counter(s.get("listing_status") for s in final_docs[market]["securities"]))
        for market in ("US", "HK")
    }
    remaining = {
        market: {
            state: sum(s.get("listing_status") == state for s in final_docs[market]["securities"])
            for state in TARGET_STATES
        }
        for market in ("US", "HK")
    }
    hk_board = [
        s for s in final_docs["HK"]["securities"]
        if s.get("board_lot_size") and s.get("board_lot_source") and s.get("board_lot_as_of")
    ]
    if len(hk_board) != len(final_docs["HK"]["securities"]):
        raise RuntimeError("HK_BOARD_LOT_FINAL_COVERAGE_FAILED")

    receipt = {
        "version": VERSION,
        "closed_at_myt": now_myt(),
        "target_count": 445,
        "results": results,
        "remaining_quarantine": remaining,
        "final_status_counts": final_counts,
        "patch_paths": patch_paths,
        "queue_rows_reconciled": queue_changed,
        "hk_board_lot": board_lot_evidence,
        "hk_board_lot_coverage": len(hk_board),
        "derived_rows": derived_rows,
        "base_complete_sha256": base_complete_sha,
        "base_checkpoint_sha256": checkpoint_before,
        "base_mutated": False,
    }
    body = compact(receipt)
    if drive.file(RECEIPT_PATH):
        old = drive.read(RECEIPT_PATH)
        drive.put(RECEIPT_PATH, body, expected_sha=digest(old))
    else:
        drive.put(RECEIPT_PATH, body)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
