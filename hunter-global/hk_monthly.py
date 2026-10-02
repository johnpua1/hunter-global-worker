"""HK monthly STOCK_EDGE recertification.

This module monthlyizes the frozen G160/G161 HK research contracts only.
It does not import US D1 and it never creates a HK vertical overlay.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from collections import defaultdict
from html.parser import HTMLParser
from typing import Any

import requests

from analytics import compose, split_adjust
from derived import read_files
from monthly_v2 import (
    WINDOW_BARS, _compact, _dynamic_split, _ema, _parse_gz_ndjson,
    _sha, _write_gz_ndjson,
)

STOPS = (1.0, 1.5, 2.0)
TARGETS = (1.5, 2.0, 3.0)
HOLDS = (5, 10, 20)
SLIPPAGE = 0.0005
HK_BUY_FEE = 0.0011
HK_SELL_FEE = 0.0011
BROKER_COMMISSION = 0.0
VALIDATION_ASOF = "2026-09-25"
VALIDATION_HKEX_DATE = "2026-09-29"
VALIDATION_LONG_PASS = 0
VALIDATION_SHORT_PASS = 0
VALIDATION_ACTIVE = 2751
VALIDATION_COMPLETE = 2484
VALIDATION_SHORT_INTERSECTION = 776
VALIDATION_SHORT_COMPLETE = 644


def _month_name(asof: str) -> str:
    d = dt.date.fromisoformat(asof)
    return f"HK_MONTH_{d.year}-{d.month:02d}"


def _snapshot_name(asof: str) -> str:
    d = dt.date.fromisoformat(asof)
    return f"HK_SNAPSHOT_{d.year}-{d.month:02d}"


def _legacy_split() -> dict[str, Any]:
    return {
        "mode": "G160_G161_FROZEN_SPLIT",
        "is_start": "2025-04-01",
        "is_end": "2025-09-30",
        "oos_start": "2025-10-01",
        "oos_end": "2026-09-22",
    }


def _segment(date: str, split: dict[str, Any]) -> str | None:
    if split["is_start"] <= date <= split["is_end"]:
        return "IS"
    if split["oos_start"] <= date <= split["oos_end"]:
        return "FINAL_OOS"
    return None


def _row_valid(row: dict) -> bool:
    try:
        o, h, l, c, v = (float(row[k]) for k in ("open", "high", "low", "close", "volume"))
    except (KeyError, TypeError, ValueError):
        return False
    return (all(math.isfinite(x) for x in (o, h, l, c, v)) and
            min(o, h, l, c) > 0 and v >= 0 and
            h >= max(o, c, l) and l <= min(o, c))


def _atr14(rows: list[dict]) -> list[float | None]:
    tr: list[float] = []
    for i, row in enumerate(rows):
        high, low = float(row["high"]), float(row["low"])
        if i == 0:
            tr.append(high - low)
        else:
            prev = float(rows[i - 1]["close"])
            tr.append(max(high - low, abs(high - prev), abs(low - prev)))
    out: list[float | None] = [None] * len(rows)
    if len(rows) < 14:
        return out
    out[13] = sum(tr[:14]) / 14.0
    for i in range(14, len(rows)):
        out[i] = (float(out[i - 1]) * 13.0 + tr[i]) / 14.0
    return out


def _signals(rows: list[dict]) -> tuple[list[int], list[int], list[float | None]]:
    closes = [float(r["close"]) for r in rows]
    e8, e17, e50 = _ema(closes, 8), _ema(closes, 17), _ema(closes, 50)
    macd = [a - b for a, b in zip(e8, e17)]
    sig9 = _ema(macd, 9)
    hist = [a - b for a, b in zip(macd, sig9)]
    long_idx: list[int] = []
    short_idx: list[int] = []
    for i in range(50, len(rows) - 1):
        if not (_row_valid(rows[i]) and _row_valid(rows[i + 1])):
            continue
        if macd[i] > 0 >= macd[i - 1] and e17[i] > e50[i] and hist[i] > 0:
            long_idx.append(i)
        if macd[i] < 0 <= macd[i - 1] and e17[i] < e50[i] and hist[i] < 0:
            short_idx.append(i)
    return long_idx, short_idx, _atr14(rows)


def _metrics(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "wr": None, "pf": None, "exp": None}
    wins = [x for x in values if x > 0]
    losses = [x for x in values if x < 0]
    # Keep JSON strict. A no-loss sample has mathematically infinite PF; store
    # the condition separately and let the gate treat it as satisfying PF.
    no_loss_profit = bool(wins and not losses)
    pf = (sum(wins) / -sum(losses)) if losses else None
    return {
        "n": len(values),
        "wr": len(wins) / len(values),
        "pf": pf,
        "pf_infinite": no_loss_profit,
        "exp": sum(values) / len(values),
    }


def _simulate_profile(rows_by_sid: dict[str, list[dict]], eligible: set[str],
                      split: dict[str, Any], side: str,
                      stop_atr: float, target_r: float, max_hold: int) -> dict[str, Any]:
    returns: dict[str, list[float]] = {"IS": [], "FINAL_OOS": []}
    for sid in sorted(eligible):
        rows = rows_by_sid.get(sid)
        if not rows or len(rows) < WINDOW_BARS:
            continue
        rows = sorted(rows, key=lambda r: r.get("trade_date", r["date"]))[-WINDOW_BARS:]
        longs, shorts, atr = _signals(rows)
        signals = longs if side == "LONG" else shorts
        opposite = set(shorts if side == "LONG" else longs)
        blocked_until = -1
        for i in signals:
            segment = _segment(rows[i].get("trade_date", rows[i]["date"]), split)
            if not segment:
                continue
            entry_i = i + 1
            if entry_i >= len(rows) or entry_i <= blocked_until:
                continue
            a = atr[i]
            if a is None or not math.isfinite(a) or a <= 0:
                continue

            signal_close = float(rows[i]["close"])
            raw_entry = float(rows[entry_i]["open"])
            if side == "LONG":
                entry = raw_entry * (1.0 + SLIPPAGE)
                stop = signal_close - stop_atr * a
                one_r = entry - stop
                if one_r <= 0:
                    continue
                target = entry + target_r * one_r
            else:
                entry = raw_entry * (1.0 - SLIPPAGE)
                stop = signal_close + stop_atr * a
                one_r = stop - entry
                if one_r <= 0:
                    continue
                target = entry - target_r * one_r

            last_i = min(entry_i + max_hold - 1, len(rows) - 1)
            exit_i = last_i
            raw_exit: float | None = None
            for j in range(entry_i, last_i + 1):
                row = rows[j]
                o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
                if side == "LONG":
                    if o <= stop:
                        raw_exit, exit_i = o, j
                        break
                    if l <= stop and h >= target:
                        raw_exit, exit_i = stop, j
                        break
                    if l <= stop:
                        raw_exit, exit_i = stop, j
                        break
                    if h >= target:
                        raw_exit, exit_i = target, j
                        break
                else:
                    if o >= stop:
                        raw_exit, exit_i = o, j
                        break
                    if h >= stop and l <= target:
                        raw_exit, exit_i = stop, j
                        break
                    if h >= stop:
                        raw_exit, exit_i = stop, j
                        break
                    if l <= target:
                        raw_exit, exit_i = target, j
                        break
                if j > entry_i and j in opposite:
                    raw_exit, exit_i = c, j
                    break

            if raw_exit is None:
                raw_exit = float(rows[exit_i]["close"])
            if side == "LONG":
                exit_fill = raw_exit * (1.0 - SLIPPAGE)
                gross_r = (exit_fill - entry) / one_r
            else:
                exit_fill = raw_exit * (1.0 + SLIPPAGE)
                gross_r = (entry - exit_fill) / one_r

            trading_cost_r = (
                HK_BUY_FEE * abs(entry) +
                HK_SELL_FEE * abs(exit_fill) +
                BROKER_COMMISSION * (abs(entry) + abs(exit_fill))
            ) / one_r
            returns[segment].append(gross_r - trading_cost_r)
            blocked_until = exit_i

    return {"IS": _metrics(returns["IS"]), "FINAL_OOS": _metrics(returns["FINAL_OOS"])}


def _pre_gate(row: dict[str, Any]) -> bool:
    oos = row["FINAL_OOS"]
    pf_ok = bool(oos.get("pf_infinite")) or (
        oos["pf"] is not None and oos["pf"] >= 1.20
    )
    if oos["n"] < 50 or not pf_ok:
        return False
    req = 0.15 if (oos["wr"] is None or oos["wr"] < 0.45) else 0.10
    return oos["exp"] is not None and oos["exp"] >= req


def _stability(grid: list[dict[str, Any]]) -> None:
    """Fail closed when the historical HK contract never reached Stability.

    G160/G161 define Stability as a required gate, but every frozen parameter
    point failed N/PF/Expectancy before Stability was evaluated. There is no
    separately frozen HK stock-Stability evaluator to reuse. Therefore this
    monthlyization preserves the historical behavior exactly:
    - pre-gate FAIL -> NOT_EVALUATED_GATE_ALREADY_FAIL;
    - pre-gate PASS -> STABILITY_CONTRACT_REQUIRED, not certified.
    A future explicit HK Stability contract can replace this fail-closed state.
    """
    for row in grid:
        if row["pre_stability_pass"]:
            row["stability"] = "STABILITY_CONTRACT_REQUIRED"
        else:
            row["stability"] = "NOT_EVALUATED_GATE_ALREADY_FAIL"
        row["status"] = "FAIL"


def edge_grid(rows_by_sid: dict[str, list[dict]], eligible: set[str],
              split: dict[str, Any], side: str) -> dict[str, Any]:
    grid: list[dict[str, Any]] = []
    for stop in STOPS:
        for target in TARGETS:
            for hold in HOLDS:
                metrics = _simulate_profile(rows_by_sid, eligible, split, side, stop, target, hold)
                oos = metrics["FINAL_OOS"]
                required_exp = 0.15 if (oos["wr"] is None or oos["wr"] < 0.45) else 0.10
                row = {
                    "side": side,
                    "stop_atr": stop,
                    "target_r": target,
                    "max_hold": hold,
                    "IS": metrics["IS"],
                    "FINAL_OOS": oos,
                    "required_exp": required_exp,
                    "n_gate": oos["n"] >= 50,
                    "pf_gate": bool(oos.get("pf_infinite")) or (
                        oos["pf"] is not None and oos["pf"] >= 1.20
                    ),
                    "exp_gate": oos["exp"] is not None and oos["exp"] >= required_exp,
                }
                row["pre_stability_pass"] = _pre_gate(row)
                grid.append(row)
    _stability(grid)
    passing = [x for x in grid if x["status"] == "PASS"]
    best = max(grid, key=lambda x: (
        -math.inf if x["FINAL_OOS"]["exp"] is None else x["FINAL_OOS"]["exp"],
        x["FINAL_OOS"]["n"],
    ))
    return {
        "side": side,
        "parameter_points": len(grid),
        "pass_count": len(passing),
        "certification": "CERTIFIED" if passing else "EDGE_NOT_CERTIFIED",
        "passing_points": passing,
        "best_by_oos_expectancy": best,
        "grid": grid,
    }


def _hk_open_dates(drive, asof: str) -> list[str]:
    dates: set[str] = set()
    for path in read_files(drive, "HK", "MARKET_CALENDAR", ".json"):
        payload = drive.json(path)
        rows = payload if isinstance(payload, list) else payload.get("sessions", [])
        for row in rows:
            date = str(row.get("date") or row.get("trade_date") or "")
            status = str(
                row.get("session_status") or row.get("status") or
                row.get("session") or row.get("market_status") or ""
            ).upper()
            if date and date <= asof and status in ("OPEN", "HALF_DAY"):
                dates.add(date)

    # The persisted BASE calendar may end at the sealed BASE as-of date.
    # Every dated HK/DAILY directory is itself evidence of a completed open
    # exchange session, so extend the calendar with those completed sessions
    # instead of guessing weekdays or holidays.
    for item in drive.list("HK/DAILY"):
        date = str(item.get("name") or "")
        if (len(date) == 10 and date[4] == "-" and date[7] == "-" and
                date <= asof):
            dates.add(date)

    if len(dates) < WINDOW_BARS:
        raise RuntimeError("HK_MARKET_CALENDAR_REQUIRES_501_OPEN_SESSIONS")
    return sorted(dates)


def _load_snapshot_groups(drive, snapshot: str) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    manifest = drive.json(snapshot + "/MANIFEST.json")
    rows: list[dict] = []
    for part in manifest["parts"]:
        rows.extend(_parse_gz_ndjson(drive.read(snapshot + "/" + part["name"])))
    by: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by[row["security_id"]].append(row)
    return {sid: sorted(v, key=lambda r: r.get("trade_date", r["date"]))
            for sid, v in by.items()}, manifest


def _build_snapshot(drive, asof: str, validation: bool = False) -> tuple[str, dict[str, list[dict]], dict]:
    snapshot = _snapshot_name(asof)
    existing = drive.file(snapshot + "/MANIFEST.json")
    if existing:
        rows, manifest = _load_snapshot_groups(drive, snapshot)
        if manifest.get("as_of") != asof:
            raise RuntimeError("HK_SNAPSHOT_MONTH_ASOF_CONFLICT:" + snapshot)
        return snapshot, rows, manifest

    universe = drive.json("HK/CURRENT_UNIVERSE.json")
    active = {x["security_id"]: x for x in universe["securities"]
              if x.get("listing_status") == "ACTIVE"}
    base_asof = drive.json("HK/CHECKPOINT.json")["as_of"]

    daily: dict[str, list[dict]] = defaultdict(list)
    for item in drive.list("HK/DAILY"):
        name = item["name"]
        if not (len(name) == 10 and name[4] == "-" and name[7] == "-"):
            continue
        if name <= base_asof or name > asof:
            continue
        for path in read_files(drive, "HK", "DAILY/" + name, (".ndjson.gz", ".ndjson.gzip")):
            for row in _parse_gz_ndjson(drive.read(path)):
                if row.get("security_id") in active:
                    daily[row["security_id"]].append(row)

    patches: dict[str, list[dict]] = defaultdict(list)
    for path in read_files(drive, "HK", "REPAIR_PATCH", ".json"):
        payload = drive.json(path)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = [payload] if isinstance(payload, dict) else []
        for patch in items:
            if patch.get("security_id") in active:
                patches[patch["security_id"]].append(patch)

    event_cutoff = "2026-09-30" if validation else asof
    events: list[dict] = []
    for path in read_files(drive, "HK", "CORPORATE_ACTIONS", ".json"):
        payload = drive.json(path)
        if isinstance(payload, list):
            events.extend(e for e in payload if e.get("effective_date", "") <= event_cutoff)

    drive.folder(snapshot, create=True)
    uni_doc = {
        "schema": "HUNTER_HK_MONTHLY_SNAPSHOT_V1",
        "snapshot_name": snapshot,
        "market": "HK",
        "as_of": asof,
        "active_count": len(active),
        "securities": [active[k] for k in sorted(active)],
    }
    drive.put(snapshot + "/HK_ACTIVE_UNIVERSE.json", _compact(uni_doc), immutable=True)

    part_manifest = []
    groups: dict[str, list[dict]] = {}
    seen: set[str] = set()
    base_files = sorted(x["name"] for x in drive.list("HK/BASE")
                        if x["name"].endswith(".ndjson.gz"))
    for part_no, name in enumerate(base_files, 1):
        by: dict[str, list[dict]] = defaultdict(list)
        for row in _parse_gz_ndjson(drive.read("HK/BASE/" + name)):
            sid = row.get("security_id")
            if sid in active:
                by[sid].append(row)
        out = []
        for sid in sorted(by):
            rows = split_adjust(compose(by[sid], patches[sid], daily[sid]), events)
            rows = [r for r in rows if r.get("trade_date", r["date"]) <= asof]
            if rows:
                rows = sorted(rows, key=lambda r: r.get("trade_date", r["date"]))
                groups[sid] = rows
                seen.add(sid)
                out.extend(rows)
        payload = _write_gz_ndjson(out)
        pname = f"HK_ACTIVE_OHLC_PART_{part_no:04d}.ndjson.gz"
        drive.put(snapshot + "/" + pname, payload, "application/x-gzip", immutable=True)
        part_manifest.append({"name": pname, "rows": len(out),
                              "bytes": len(payload), "sha256": _sha(payload)})

    tail = []
    for sid in sorted(set(active) - seen):
        rows = split_adjust(compose([], patches[sid], daily[sid]), events)
        rows = [r for r in rows if r.get("trade_date", r["date"]) <= asof]
        if rows:
            rows = sorted(rows, key=lambda r: r.get("trade_date", r["date"]))
            groups[sid] = rows
            tail.extend(rows)
    if tail:
        payload = _write_gz_ndjson(tail)
        pname = f"HK_ACTIVE_OHLC_PART_{len(part_manifest)+1:04d}.ndjson.gz"
        drive.put(snapshot + "/" + pname, payload, "application/x-gzip", immutable=True)
        part_manifest.append({"name": pname, "rows": len(tail),
                              "bytes": len(payload), "sha256": _sha(payload)})

    complete = sum(len(v) >= WINDOW_BARS for v in groups.values())
    manifest = {
        "schema": "HUNTER_HK_MONTHLY_SNAPSHOT_V1",
        "snapshot": snapshot,
        "market": "HK",
        "as_of": asof,
        "semantic": "IMMUTABLE_READ_ONLY_BY_NAME_AND_SHA256",
        "active_count": len(active),
        "complete_501_count": complete,
        "parts": part_manifest,
    }
    drive.put(snapshot + "/MANIFEST.json", _compact(manifest), immutable=True)
    return snapshot, groups, manifest


class _HKEXParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_td = False
        self.cell = []
        self.row = []
        self.rows = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "tr":
            self.in_tr = True
            self.row = []
        elif tag.lower() in ("td", "th") and self.in_tr:
            self.in_td = True
            self.cell = []

    def handle_data(self, data):
        value = " ".join(data.split())
        if value:
            self.text.append(value)
            if self.in_td:
                self.cell.append(value)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_td:
            self.row.append(" ".join(self.cell).strip())
            self.in_td = False
        elif tag == "tr" and self.in_tr:
            if self.row:
                self.rows.append(self.row)
            self.in_tr = False


def _fetch_hkex_page(date_text: str) -> tuple[list[dict], dict]:
    stamp = date_text.replace("-", "")
    url = f"https://www.hkex.com.hk/eng/market/sec_tradinfo/ds{stamp}.htm"
    response = requests.get(url, timeout=45, headers={"User-Agent": "Mozilla/5.0 HunterMonthly/1.0"})
    if response.status_code != 200:
        raise RuntimeError("HKEX_HTTP_" + str(response.status_code))
    parser = _HKEXParser()
    parser.feed(response.text)
    joined = " ".join(parser.text)
    if "Designated Securities Eligible for Short Selling" not in joined:
        raise RuntimeError("HKEX_PAGE_NOT_DESIGNATED_LIST")
    match = re.search(r"Effective\s*Date\s*:?\s*(\d{2}/\d{2}/\d{4})", joined, re.I)
    if not match:
        raise RuntimeError("HKEX_EFFECTIVE_DATE_MISSING")
    dd, mm, yyyy = match.group(1).split("/")
    effective = f"{yyyy}-{mm}-{dd}"
    rows = []
    for cells in parser.rows:
        if len(cells) < 3:
            continue
        first = re.sub(r"\D", "", cells[0])
        second = re.sub(r"\D", "", cells[1])
        if not first or not second:
            continue
        try:
            no = int(first)
            code = int(second)
        except ValueError:
            continue
        rows.append({
            "no": no,
            "stock_code": code,
            "stock_short_name": cells[2].strip(),
            "tick_rule_exemption": cells[3].strip() if len(cells) > 3 else "",
            "effective_date": effective,
            "source_url": url,
        })
    if len(rows) < 500:
        raise RuntimeError("HKEX_LIST_ROW_COUNT_SUSPECT:" + str(len(rows)))
    return rows, {"effective_date": effective, "source_url": url, "official_total": len(rows)}


def _latest_hkex(asof: str, validation: bool = False) -> tuple[list[dict], dict]:
    if validation:
        return _fetch_hkex_page(VALIDATION_HKEX_DATE)
    date = dt.date.fromisoformat(asof)
    errors = []
    for offset in range(0, 35):
        candidate = (date - dt.timedelta(days=offset)).isoformat()
        try:
            rows, version = _fetch_hkex_page(candidate)
            if version["effective_date"] <= asof:
                return rows, version
        except Exception as exc:
            errors.append(str(exc)[:80])
    raise RuntimeError("HKEX_LATEST_DESIGNATED_LIST_NOT_FOUND:" + "|".join(errors[-5:]))


def _csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "no", "stock_code", "stock_short_name", "tick_rule_exemption",
        "effective_date", "source_url",
    ], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _grid_csv(edge: dict[str, Any]) -> bytes:
    buf = io.StringIO()
    fields = [
        "side", "stop_atr", "target_r", "max_hold",
        "is_n", "is_wr", "is_pf", "is_exp",
        "oos_n", "oos_wr", "oos_pf", "oos_exp", "required_exp",
        "n_gate", "pf_gate", "exp_gate", "pre_stability_pass",
        "stability", "status",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in edge["grid"]:
        writer.writerow({
            "side": row["side"], "stop_atr": row["stop_atr"],
            "target_r": row["target_r"], "max_hold": row["max_hold"],
            "is_n": row["IS"]["n"], "is_wr": row["IS"]["wr"],
            "is_pf": row["IS"]["pf"], "is_exp": row["IS"]["exp"],
            "oos_n": row["FINAL_OOS"]["n"], "oos_wr": row["FINAL_OOS"]["wr"],
            "oos_pf": row["FINAL_OOS"]["pf"], "oos_exp": row["FINAL_OOS"]["exp"],
            "required_exp": row["required_exp"], "n_gate": row["n_gate"],
            "pf_gate": row["pf_gate"], "exp_gate": row["exp_gate"],
            "pre_stability_pass": row["pre_stability_pass"],
            "stability": row["stability"], "status": row["status"],
        })
    return buf.getvalue().encode("utf-8")


def _edge_summary(edge: dict[str, Any]) -> dict[str, Any]:
    best = edge["best_by_oos_expectancy"]
    return {
        "status": edge["certification"],
        "pass_count": edge["pass_count"],
        "parameter_points": edge["parameter_points"],
        "best_by_oos_expectancy": {
            "stop_atr": best["stop_atr"],
            "target_r": best["target_r"],
            "max_hold": best["max_hold"],
            "n": best["FINAL_OOS"]["n"],
            "wr": best["FINAL_OOS"]["wr"],
            "pf": best["FINAL_OOS"]["pf"],
            "exp": best["FINAL_OOS"]["exp"],
            "stability": best["stability"],
            "status": best["status"],
        },
    }


def _update_pointer_and_notice(drive, month: str, snapshot: str, asof: str,
                               long_edge: dict, short_edge: dict, short_version: dict,
                               created_at_myt: str) -> dict:
    raw = drive.read("ACTIVE_POINTER") if drive.file("ACTIVE_POINTER") else None
    pointer = json.loads(raw) if raw else {}
    previous_hk = {
        "hk_month_file": pointer.get("hk_month_file"),
        "hk_snapshot": pointer.get("hk_snapshot"),
        "hk_as_of": pointer.get("hk_as_of"),
        "hk_long_result": pointer.get("hk_long_result"),
        "hk_short_result": pointer.get("hk_short_result"),
        "hk_shortable_list_version": pointer.get("hk_shortable_list_version"),
    }
    new_pointer = dict(pointer)
    new_pointer.update({
        "schema": "INVESTMENT_V2_ACTIVE_POINTER_V4",
        "hk_month_file": month,
        "hk_snapshot": snapshot,
        "hk_as_of": asof,
        "hk_long_result": _edge_summary(long_edge),
        "hk_short_result": _edge_summary(short_edge),
        "hk_shortable_list_version": short_version,
        "hk_vertical_overlay": "N/A",
        "hk_previous": previous_hk,
        "month_update_pending": True,
        "writer": "HUNTER_MONTHLY_V2_US_HK",
        "written_at_myt": created_at_myt,
    })
    if raw is None or _compact(pointer) != _compact(new_pointer):
        drive.put("ACTIVE_POINTER", _compact(new_pointer),
                  expected_sha=_sha(raw) if raw is not None else None)

    us_notice_path = "US/CONTROL/MONTH_NOTICE.json"
    us_notice_raw = drive.read(us_notice_path) if drive.file(us_notice_path) else None
    us_notice = json.loads(us_notice_raw) if us_notice_raw else {}
    combined = dict(us_notice)
    combined["schema"] = "INVESTMENT_V2_MONTH_NOTICE_V3"
    combined["pending"] = True
    combined["hk_previous"] = previous_hk
    combined["hk_current"] = {
        "hk_month_file": month,
        "hk_snapshot": snapshot,
        "hk_as_of": asof,
        "hk_long_result": new_pointer["hk_long_result"],
        "hk_short_result": new_pointer["hk_short_result"],
        "hk_shortable_list_version": short_version,
        "hk_vertical_overlay": "N/A",
    }
    drive.put(us_notice_path, _compact(combined),
              expected_sha=_sha(us_notice_raw) if us_notice_raw is not None else None)

    hk_notice_path = "HK/CONTROL/MONTH_NOTICE.json"
    hk_notice_raw = drive.read(hk_notice_path) if drive.file(hk_notice_path) else None
    drive.put(hk_notice_path, _compact(combined),
              expected_sha=_sha(hk_notice_raw) if hk_notice_raw is not None else None)
    return new_pointer


def run_hk_monthly(drive, validation: bool = False) -> dict[str, Any]:
    if validation:
        asof = VALIDATION_ASOF
    else:
        checkpoint = drive.json("HK/CONTROL/DAILY_CHECKPOINT.json")
        asof = checkpoint.get("last_completed_date") or checkpoint.get("as_of")
        if not asof:
            raise RuntimeError("HK_DAILY_CHECKPOINT_DATE_MISSING")

    snapshot, groups, manifest = _build_snapshot(drive, asof, validation=validation)
    complete_rows = {sid: rows for sid, rows in groups.items() if len(rows) >= WINDOW_BARS}
    if validation:
        split = _legacy_split()
    else:
        split = _dynamic_split(_hk_open_dates(drive, asof))

    hkex_rows, short_version = _latest_hkex(asof, validation=validation)
    short_filename = "HKEX_DESIGNATED_SHORT_SELLING_" + short_version["effective_date"].replace("-", "") + ".csv"
    short_payload = _csv_bytes(hkex_rows)
    short_path = snapshot + "/" + short_filename
    if drive.file(short_path):
        if _sha(drive.read(short_path)) != _sha(short_payload):
            raise RuntimeError("HKEX_IMMUTABLE_ARCHIVE_MISMATCH")
    else:
        drive.put(short_path, short_payload, "text/plain", immutable=True)

    universe = drive.json(snapshot + "/HK_ACTIVE_UNIVERSE.json")
    active = {x["security_id"]: x for x in universe["securities"]}
    standard_codes = {int(row["stock_code"]) for row in hkex_rows if int(row["stock_code"]) < 10000}
    short_ids = {sid for sid, security in active.items()
                 if int(security.get("stock_code", -1)) in standard_codes}
    short_complete = {sid for sid in short_ids if sid in complete_rows}

    long_edge = edge_grid(complete_rows, set(complete_rows), split, "LONG")
    short_edge = edge_grid(complete_rows, short_complete, split, "SHORT")

    short_version = {
        **short_version,
        "archive_file": short_filename,
        "standard_stock_code_subset": len(standard_codes),
        "active_intersection": len(short_ids),
        "complete_501": len(short_complete),
    }

    validation_result = {
        "pass": None,
        "expected": None,
        "observed": None,
    }
    if validation:
        observed = {
            "active": manifest["active_count"],
            "complete_501": len(complete_rows),
            "short_active_intersection": len(short_ids),
            "short_complete_501": len(short_complete),
            "long_pass": long_edge["pass_count"],
            "short_pass": short_edge["pass_count"],
        }
        expected = {
            "active": VALIDATION_ACTIVE,
            "complete_501": VALIDATION_COMPLETE,
            "short_active_intersection": VALIDATION_SHORT_INTERSECTION,
            "short_complete_501": VALIDATION_SHORT_COMPLETE,
            "long_pass": VALIDATION_LONG_PASS,
            "short_pass": VALIDATION_SHORT_PASS,
        }
        validation_result = {"pass": observed == expected,
                             "expected": expected, "observed": observed}
        if not validation_result["pass"]:
            raise RuntimeError("G160_G161_VALIDATION_MISMATCH:" +
                               json.dumps(validation_result, ensure_ascii=False, sort_keys=True))

    month = _month_name(asof)
    result_path = month + "/RESULT.json"
    existing = drive.json(result_path) if drive.file(result_path) else None
    now = (existing or {}).get("created_at_myt") or dt.datetime.now(
        dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    result = {
        "schema": "HUNTER_HK_MONTHLY_V1",
        "market": "HK",
        "month": month,
        "snapshot": snapshot,
        "as_of": asof,
        "created_at_myt": now,
        "split": split,
        "active_count": manifest["active_count"],
        "complete_501_count": len(complete_rows),
        "hkex_shortable": short_version,
        "long": _edge_summary(long_edge),
        "short": _edge_summary(short_edge),
        "cost_model": {
            "slippage_bps_per_side": 5,
            "hk_buy_fee_rate": HK_BUY_FEE,
            "hk_sell_fee_rate": HK_SELL_FEE,
            "broker_commission_rate": BROKER_COMMISSION,
            "borrow_cost_model": "BORROW_COST_NOT_MODELED",
        },
        "hk_vertical_overlay": "N/A",
        "validation": validation_result,
    }
    if existing is not None and _compact(existing) != _compact(result):
        raise RuntimeError("HK_MONTH_IMMUTABLE_RESULT_MISMATCH:" + month)

    drive.folder(month, create=True)
    drive.put(result_path, _compact(result), immutable=True)
    drive.put(month + "/HK_LONG_GRID.csv", _grid_csv(long_edge), "text/plain", immutable=True)
    drive.put(month + "/HK_SHORT_GRID.csv", _grid_csv(short_edge), "text/plain", immutable=True)

    pointer = None
    if not validation:
        pointer = _update_pointer_and_notice(
            drive, month, snapshot, asof, long_edge, short_edge, short_version, now
        )
    else:
        drive.put(month + "/VALIDATION_G160_G161.json",
                  _compact(validation_result), immutable=True)

    return {
        "month": month,
        "snapshot": snapshot,
        "as_of": asof,
        "long": _edge_summary(long_edge),
        "short": _edge_summary(short_edge),
        "shortable": short_version,
        "validation": validation_result,
        "pointer": pointer,
    }
