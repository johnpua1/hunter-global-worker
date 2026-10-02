"""HK monthly GLOBAL-V1 stock-edge recertification.

This module monthly-izes the frozen HK LONG/SHORT research contract only.
It does not import or reuse the US D1 direction layer and does not invent a
new parameter-stability algorithm. When N/PF/Expectancy pre-gates pass but the
frozen stock stability evaluator is unavailable, certification fails closed.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import math
import re
from collections import defaultdict
from html.parser import HTMLParser
from typing import Any, Iterable

import requests

WINDOW_BARS = 501
PRE_RESEARCH_BARS = 6
RESEARCH_BARS = 128
VALIDATION_BARS = 125
FINAL_OOS_BARS = 239
TAIL_BARS = 3
assert PRE_RESEARCH_BARS + RESEARCH_BARS + VALIDATION_BARS + FINAL_OOS_BARS + TAIL_BARS == WINDOW_BARS

STOPS = (1.0, 1.5, 2.0)
TARGETS = (1.5, 2.0, 3.0)
HOLDS = (5, 10, 20)
SLIPPAGE_RATE = 0.0005
HK_FEE_BUY_RATE = 0.0011
HK_FEE_SELL_RATE = 0.0011
BROKER_COMMISSION_RATE = 0.0
BORROW_COST_MODEL = "BORROW_COST_NOT_MODELED"
HK_VERTICAL_OVERLAY = "N/A"


def _compact(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_gz_ndjson(rows: list[dict]) -> bytes:
    raw = b"\n".join(_compact(row) for row in rows) + (b"\n" if rows else b"")
    return gzip.compress(raw, mtime=0)


def _parse_gz_ndjson(data: bytes) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(data).splitlines() if line]


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


def _split_calendar(open_dates: list[str]) -> dict[str, Any]:
    dates = sorted(dict.fromkeys(open_dates))
    if len(dates) < WINDOW_BARS:
        raise RuntimeError("HK_MONTH_SPLIT_REQUIRES_501_OPEN_SESSIONS")
    window = dates[-WINDOW_BARS:]
    i = PRE_RESEARCH_BARS
    research = window[i:i + RESEARCH_BARS]
    i += RESEARCH_BARS
    validation = window[i:i + VALIDATION_BARS]
    i += VALIDATION_BARS
    final_oos = window[i:i + FINAL_OOS_BARS]
    i += FINAL_OOS_BARS
    tail = window[i:i + TAIL_BARS]
    if not all((research, validation, final_oos, tail)) or i + TAIL_BARS != WINDOW_BARS:
        raise RuntimeError("HK_MONTH_SPLIT_SHAPE_DRIFT")
    return {
        "mode": "HK_G160_G161_RATIO_RECUT",
        "window_bars": WINDOW_BARS,
        "pre_research_bars": PRE_RESEARCH_BARS,
        "research_bars": RESEARCH_BARS,
        "validation_bars": VALIDATION_BARS,
        "final_oos_bars": FINAL_OOS_BARS,
        "tail_bars": TAIL_BARS,
        "window_start": window[0],
        "window_end": window[-1],
        "research_start": research[0],
        "research_end": research[-1],
        "validation_start": validation[0],
        "validation_end": validation[-1],
        "oos_start": final_oos[0],
        "oos_end": final_oos[-1],
        "tail_start": tail[0],
        "tail_end": tail[-1],
        "open_dates": window,
    }


def _segment(date: str, split: dict[str, Any]) -> str | None:
    if split["research_start"] <= date <= split["research_end"]:
        return "RESEARCH"
    if split["validation_start"] <= date <= split["validation_end"]:
        return "VALIDATION"
    if split["oos_start"] <= date <= split["oos_end"]:
        return "FINAL_OOS"
    return None


def _atr14_wilder(rows: list[dict]) -> list[float | None]:
    tr: list[float] = []
    previous = None
    for row in rows:
        high, low, close = (float(row[k]) for k in ("high", "low", "close"))
        tr.append(high - low if previous is None else max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    out: list[float | None] = [None] * len(rows)
    if len(tr) >= 14:
        out[13] = sum(tr[:14]) / 14.0
        for i in range(14, len(tr)):
            out[i] = (float(out[i - 1]) * 13.0 + tr[i]) / 14.0
    return out


def _features(rows: list[dict]) -> tuple[list[float | None], list[bool], list[bool]]:
    closes = [float(row["close"]) for row in rows]
    e8, e17, e50 = _ema(closes, 8), _ema(closes, 17), _ema(closes, 50)
    macd = [a - b for a, b in zip(e8, e17)]
    signal9 = _ema(macd, 9)
    histogram = [a - b for a, b in zip(macd, signal9)]
    long_signal = [False] * len(rows)
    short_signal = [False] * len(rows)
    for i in range(50, len(rows)):
        long_signal[i] = macd[i] > 0 >= macd[i - 1] and e17[i] > e50[i] and histogram[i] > 0
        short_signal[i] = macd[i] < 0 <= macd[i - 1] and e17[i] < e50[i] and histogram[i] < 0
    return _atr14_wilder(rows), long_signal, short_signal


def _slipped(price: float, side: int, entry: bool) -> float:
    adverse_up = (entry and side == 1) or ((not entry) and side == -1)
    return price * (1.0 + SLIPPAGE_RATE if adverse_up else 1.0 - SLIPPAGE_RATE)


def _simulate(rows: list[dict], features, split: dict[str, Any], stop_atr: float,
              target_r: float, max_hold: int, side: int) -> list[dict]:
    atr, longs, shorts = features
    signals = longs if side == 1 else shorts
    opposite = shorts if side == 1 else longs
    trades: list[dict] = []
    free_after = -1
    for i in range(50, len(rows) - 1):
        segment = _segment(rows[i]["date"], split)
        if segment is None or not signals[i] or i < free_after:
            continue
        a = atr[i]
        if a is None or not math.isfinite(a) or a <= 0:
            continue
        signal_close = float(rows[i]["close"])
        entry_i = i + 1
        entry = _slipped(float(rows[entry_i]["open"]), side, True)
        stop = signal_close - side * stop_atr * a
        if (side == 1 and entry <= stop) or (side == -1 and entry >= stop):
            continue
        risk = abs(entry - stop)
        if not risk > 0:
            continue
        target = entry + side * target_r * risk
        max_exit_i = entry_i + max_hold - 1
        if max_exit_i >= len(rows):
            continue
        exit_price = None
        exit_i = None
        reason = None
        for j in range(entry_i, max_exit_i + 1):
            row = rows[j]
            opn, high, low, close = (float(row[k]) for k in ("open", "high", "low", "close"))
            if (opn <= stop if side == 1 else opn >= stop):
                exit_price, exit_i, reason = _slipped(opn, side, False), j, "STOP_GAP"
                break
            if (opn >= target if side == 1 else opn <= target):
                exit_price, exit_i, reason = _slipped(target, side, False), j, "TARGET_GAP"
                break
            if (low <= stop if side == 1 else high >= stop):
                exit_price, exit_i, reason = _slipped(stop, side, False), j, "STOP"
                break
            if (high >= target if side == 1 else low <= target):
                exit_price, exit_i, reason = _slipped(target, side, False), j, "TARGET"
                break
            if opposite[j] and j < max_exit_i:
                exit_price, exit_i, reason = _slipped(close, side, False), j, "OPPOSITE"
                break
            if j == max_exit_i:
                exit_price, exit_i, reason = _slipped(close, side, False), j, "HOLD"
                break
        if exit_price is None or exit_i is None:
            continue
        gross_r = (exit_price - entry) * side / risk
        fees_r = (entry * HK_FEE_BUY_RATE + exit_price * HK_FEE_SELL_RATE) / risk
        trades.append({
            "segment": segment,
            "signal_date": rows[i]["date"],
            "entry_date": rows[entry_i]["date"],
            "exit_date": rows[exit_i]["date"],
            "r_net": gross_r - fees_r,
            "reason": reason,
        })
        free_after = exit_i
    return trades


def _metrics(values: list[float]) -> dict[str, Any]:
    n = len(values)
    if not n:
        return {"n": 0, "wr": None, "pf": None, "expectancy_r": None}
    positive = sum(x for x in values if x > 0)
    negative = -sum(x for x in values if x < 0)
    return {
        "n": n,
        "wr": sum(x > 0 for x in values) / n,
        "pf": positive / negative if negative else math.inf,
        "expectancy_r": sum(values) / n,
    }


def _gate(metrics: dict[str, Any]) -> dict[str, Any]:
    wr = metrics["wr"]
    required_exp = 0.15 if wr is not None and wr < 0.45 else 0.10
    n_gate = metrics["n"] >= 50
    pf_gate = metrics["pf"] is not None and metrics["pf"] >= 1.20
    exp_gate = metrics["expectancy_r"] is not None and metrics["expectancy_r"] >= required_exp
    pre_stability_pass = bool(n_gate and pf_gate and exp_gate)
    if not pre_stability_pass:
        stability = "NOT_EVALUATED_GATE_ALREADY_FAIL"
        fail = []
        if not n_gate:
            fail.append("N_LT_50")
        if not pf_gate:
            fail.append("PF_LT_1.20")
        if not exp_gate:
            fail.append(f"EXP_LT_{required_exp:.2f}")
    else:
        stability = "NOT_CERTIFIED_FROZEN_EVALUATOR_UNAVAILABLE"
        fail = ["STABILITY_NOT_CERTIFIED"]
    return {
        "required_exp": required_exp,
        "n_gate": n_gate,
        "pf_gate": pf_gate,
        "exp_gate": exp_gate,
        "pre_stability_pass": pre_stability_pass,
        "stability": stability,
        "status": "FAIL",
        "high_win_55_label": bool(wr is not None and wr >= 0.55),
        "fail_reason": "|".join(fail),
    }


def _grid(groups: dict[str, list[dict]], split: dict[str, Any], side: int,
          eligible_ids: Iterable[str]) -> list[dict]:
    ids = sorted(eligible_ids)
    feature_cache = {sid: _features(groups[sid]) for sid in ids}
    out = []
    for stop in STOPS:
        for target in TARGETS:
            for hold in HOLDS:
                validation_values: list[float] = []
                oos_values: list[float] = []
                for sid in ids:
                    trades = _simulate(groups[sid], feature_cache[sid], split, stop, target, hold, side)
                    validation_values.extend(t["r_net"] for t in trades if t["segment"] == "VALIDATION")
                    oos_values.extend(t["r_net"] for t in trades if t["segment"] == "FINAL_OOS")
                validation = _metrics(validation_values)
                oos = _metrics(oos_values)
                out.append({
                    "stop_atr": stop,
                    "target_r": target,
                    "max_hold": hold,
                    "validation": validation,
                    "final_oos": oos,
                    **_gate(oos),
                })
    return out


def _summarize(grid: list[dict], eligible: int, side: str) -> dict[str, Any]:
    passed = [row for row in grid if row["status"] == "PASS"]
    best = max(grid, key=lambda row: row["final_oos"]["expectancy_r"]
               if row["final_oos"]["expectancy_r"] is not None else -math.inf)
    return {
        "side": side,
        "parameter_points": len(grid),
        "eligible_full_501": eligible,
        "pass_count": len(passed),
        "status": "CERTIFIED" if passed else "EDGE_NOT_CERTIFIED",
        "best_final_oos": best,
        "high_win_count": sum(1 for row in grid if row["high_win_55_label"]),
        "stability_fail_closed_count": sum(
            1 for row in grid if row["pre_stability_pass"] and row["status"] != "PASS"),
    }


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_cell = False
        self.cell = []
        self.row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("td", "th"):
            self.in_cell = True
            self.cell = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            self.row.append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        elif tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.row = []


def _parse_hkex_codes(html: str) -> list[str]:
    parser = _TableParser()
    parser.feed(html)
    codes = set()
    for row in parser.rows:
        for cell in row:
            value = cell.replace(",", "").strip()
            if re.fullmatch(r"\d{5}", value):
                codes.add(value)
                break
    if not codes:
        codes.update(re.findall(r"(?<!\d)(\d{5})(?!\d)", html))
    return sorted(codes)


def _download_hkex(asof: str) -> dict[str, Any]:
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 HunterMonthly/1.0", "Accept": "text/html,*/*"}
    day = dt.date.fromisoformat(asof)
    errors = []
    for offset in range(45):
        candidate = day - dt.timedelta(days=offset)
        stamp = candidate.strftime("%Y%m%d")
        url = f"https://www.hkex.com.hk/eng/market/sec_tradinfo/ds{stamp}.htm"
        try:
            response = session.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                errors.append(f"{stamp}:{response.status_code}")
                continue
            codes = _parse_hkex_codes(response.text)
            if len(codes) < 100:
                errors.append(f"{stamp}:PARSED_{len(codes)}")
                continue
            return {
                "version": f"HKEX_DESIGNATED_SHORT_SELLING_{stamp}",
                "effective_date": candidate.isoformat(),
                "official_url": url,
                "codes": codes,
                "raw_html": response.text,
            }
        except requests.RequestException as exc:
            errors.append(f"{stamp}:{type(exc).__name__}")
    raise RuntimeError("HKEX_DESIGNATED_LIST_REFRESH_FAILED:" + ",".join(errors[-8:]))


def _open_sessions(drive, asof: str) -> list[str]:
    dates = [row["date"] for row in _parse_gz_ndjson(drive.read("HK/CALENDAR_BASE.ndjson.gz"))]
    base_asof = drive.json("HK/CHECKPOINT.json")["as_of"]
    for item in drive.list("HK/DAILY"):
        name = item["name"]
        if len(name) == 10 and name[4] == "-" and name[7] == "-" and base_asof < name <= asof:
            dates.append(name)
    return sorted(dict.fromkeys(dates))


def _load_snapshot(drive, snapshot: str) -> tuple[dict, dict[str, list[dict]], dict[str, dict]]:
    manifest = drive.json(snapshot + "/MANIFEST.json")
    rows = []
    for item in drive.list(snapshot):
        name = item["name"]
        if name.startswith("HK_ACTIVE_OHLC_PART_") and name.endswith(".ndjson.gz"):
            rows.extend(_parse_gz_ndjson(drive.read(snapshot + "/" + name)))
    by = defaultdict(list)
    for row in rows:
        by[row["security_id"]].append(row)
    groups = {sid: sorted(value, key=lambda x: x["date"]) for sid, value in by.items()}
    universe_doc = drive.json(snapshot + "/HK_ACTIVE_UNIVERSE.json")
    securities = {row["security_id"]: row for row in universe_doc["securities"]}
    return manifest, groups, securities


def _build_snapshot(drive, asof: str) -> tuple[str, dict, dict[str, list[dict]], dict[str, dict]]:
    from analytics import compose, split_adjust
    from derived import read_files

    month = asof[:7]
    snapshot = "HK_SNAPSHOT_" + month
    if drive.file(snapshot + "/MANIFEST.json"):
        manifest, groups, securities = _load_snapshot(drive, snapshot)
        if manifest.get("month") != month:
            raise RuntimeError("HK_SNAPSHOT_MONTH_CONFLICT")
        return snapshot, manifest, groups, securities

    universe = drive.json("HK/CURRENT_UNIVERSE.json")
    active = {row["security_id"]: row for row in universe["securities"]
              if row.get("listing_status") == "ACTIVE"}
    base_asof = drive.json("HK/CHECKPOINT.json")["as_of"]

    daily = defaultdict(list)
    for item in drive.list("HK/DAILY"):
        name = item["name"]
        if not (len(name) == 10 and name[4] == "-" and name[7] == "-") or not (base_asof < name <= asof):
            continue
        for path in read_files(drive, "HK", "DAILY/" + name, (".ndjson.gz", ".ndjson.gzip")):
            for row in _parse_gz_ndjson(drive.read(path)):
                if row.get("security_id") in active:
                    daily[row["security_id"]].append(row)

    patches = defaultdict(list)
    for path in read_files(drive, "HK", "REPAIR_PATCH", ".json"):
        payload = drive.json(path)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = [payload] if isinstance(payload, dict) else []
        for patch in items:
            if patch.get("security_id") in active:
                patches[patch["security_id"]].append(patch)

    events = []
    for path in read_files(drive, "HK", "CORPORATE_ACTIONS", ".json"):
        payload = drive.json(path)
        if isinstance(payload, list):
            events.extend(payload)

    split = _split_calendar(_open_sessions(drive, asof))
    hkex = _download_hkex(asof)

    drive.folder(snapshot, create=True)
    drive.put(snapshot + "/HK_ACTIVE_UNIVERSE.json", _compact({
        "schema": "HK_MONTHLY_SNAPSHOT_V1",
        "snapshot": snapshot,
        "market": "HK",
        "month": month,
        "as_of": asof,
        "active_count": len(active),
        "securities": [active[sid] for sid in sorted(active)],
    }), immutable=True)

    groups: dict[str, list[dict]] = {}
    part_manifest = []
    seen = set()
    base_files = sorted(item["name"] for item in drive.list("HK/BASE")
                        if item["name"].endswith(".ndjson.gz"))
    for part_no, name in enumerate(base_files, 1):
        base_by = defaultdict(list)
        for row in _parse_gz_ndjson(drive.read("HK/BASE/" + name)):
            sid = row.get("security_id")
            if sid in active:
                base_by[sid].append(row)
        out = []
        for sid in sorted(base_by):
            rows = split_adjust(compose(base_by[sid], patches[sid], daily[sid]), events)
            rows = [row for row in rows if row.get("trade_date", row["date"]) <= asof]
            if rows:
                groups[sid] = rows
                seen.add(sid)
                out.extend(rows)
        payload = _write_gz_ndjson(out)
        pname = f"HK_ACTIVE_OHLC_PART_{part_no:04d}.ndjson.gz"
        drive.put(snapshot + "/" + pname, payload, "application/x-gzip", immutable=True)
        part_manifest.append({"name": pname, "rows": len(out), "bytes": len(payload), "sha256": _sha(payload)})

    tail = []
    for sid in sorted(set(active) - seen):
        rows = split_adjust(compose([], patches[sid], daily[sid]), events)
        rows = [row for row in rows if row.get("trade_date", row["date"]) <= asof]
        if rows:
            groups[sid] = rows
            tail.extend(rows)
    if tail:
        payload = _write_gz_ndjson(tail)
        pname = f"HK_ACTIVE_OHLC_PART_{len(part_manifest)+1:04d}.ndjson.gz"
        drive.put(snapshot + "/" + pname, payload, "application/x-gzip", immutable=True)
        part_manifest.append({"name": pname, "rows": len(tail), "bytes": len(payload), "sha256": _sha(payload)})

    parsed_path = snapshot + "/" + hkex["version"] + ".json"
    raw_path = snapshot + "/" + hkex["version"] + ".html"
    drive.put(parsed_path, _compact({
        "schema": "HKEX_DESIGNATED_SHORT_SELLING_V1",
        "version": hkex["version"],
        "effective_date": hkex["effective_date"],
        "official_url": hkex["official_url"],
        "codes": hkex["codes"],
    }), immutable=True)
    drive.put(raw_path, hkex["raw_html"].encode("utf-8"), "text/plain", immutable=True)
    drive.put(snapshot + "/HK_CALENDAR_501.json", _compact({"split": split}), immutable=True)

    manifest = {
        "schema": "HK_MONTHLY_SNAPSHOT_V1",
        "snapshot": snapshot,
        "market": "HK",
        "month": month,
        "as_of": asof,
        "active_count": len(active),
        "calendar": split,
        "ohlc_parts": part_manifest,
        "hkex_designated": {
            "version": hkex["version"],
            "effective_date": hkex["effective_date"],
            "official_url": hkex["official_url"],
            "parsed_count": len(hkex["codes"]),
            "parsed_path": parsed_path,
            "raw_path": raw_path,
        },
    }
    drive.put(snapshot + "/MANIFEST.json", _compact(manifest), immutable=True)
    return snapshot, manifest, groups, active


def _eligible_groups(groups: dict[str, list[dict]], split: dict[str, Any]) -> dict[str, list[dict]]:
    expected = set(split["open_dates"])
    out = {}
    for sid, rows in groups.items():
        if expected.issubset({row["date"] for row in rows}):
            out[sid] = sorted(rows, key=lambda x: x["date"])
    return out


def run_hk_monthly(drive, validation: bool = False) -> dict[str, Any]:
    checkpoint = drive.json("HK/CONTROL/DAILY_CHECKPOINT.json")
    current_asof = checkpoint.get("last_completed_date") or checkpoint.get("as_of")
    if not current_asof:
        raise RuntimeError("HK_DAILY_CHECKPOINT_DATE_MISSING")

    snapshot, manifest, groups, securities = _build_snapshot(drive, current_asof)
    asof = manifest["as_of"]
    split = manifest["calendar"]
    eligible = _eligible_groups(groups, split)

    designated = drive.json(manifest["hkex_designated"]["parsed_path"])
    designated_codes = {int(code) for code in designated["codes"]}
    short_ids = {
        sid for sid in eligible
        if sid in securities and int(securities[sid]["stock_code"]) in designated_codes
    }

    long_grid = _grid(eligible, split, 1, eligible.keys())
    short_grid = _grid(eligible, split, -1, short_ids)
    long_result = _summarize(long_grid, len(eligible), "LONG")
    short_result = _summarize(short_grid, len(short_ids), "SHORT")

    regression = {
        "expected": {"HK_LONG_PASS_COUNT": 0, "HK_SHORT_PASS_COUNT": 0},
        "observed": {
            "HK_LONG_PASS_COUNT": long_result["pass_count"],
            "HK_SHORT_PASS_COUNT": short_result["pass_count"],
        },
    }
    regression["pass"] = regression["observed"] == regression["expected"]
    if validation and not regression["pass"]:
        raise RuntimeError("HK_G160_G161_REGRESSION_MISMATCH:" +
                           json.dumps(regression, sort_keys=True))

    month = "HK_MONTH_" + asof[:7]
    result = {
        "schema": "HK_MONTHLY_STOCK_EDGE_V1",
        "month": month,
        "snapshot": snapshot,
        "as_of": asof,
        "split": split,
        "hkex_designated": manifest["hkex_designated"],
        "costs": {
            "slippage_bps_per_side": 5,
            "hk_fee_buy_rate": HK_FEE_BUY_RATE,
            "hk_fee_sell_rate": HK_FEE_SELL_RATE,
            "broker_commission_rate": BROKER_COMMISSION_RATE,
            "borrow_cost_model": BORROW_COST_MODEL,
        },
        "long": long_result,
        "short": short_result,
        "hk_vertical_overlay": HK_VERTICAL_OVERLAY,
        "regression": regression,
        "stability_policy": "FROZEN_EVALUATOR_REQUIRED_FAIL_CLOSED_NO_NEW_ALGORITHM",
    }
    result_path = month + "/RESULT.json"
    existing = drive.json(result_path) if drive.file(result_path) else None
    if existing is not None and _compact(existing) != _compact(result):
        raise RuntimeError("HK_MONTH_IMMUTABLE_RESULT_MISMATCH:" + month)
    drive.folder(month, create=True)
    drive.put(result_path, _compact(result), immutable=True)
    drive.put(month + "/LONG_GRID.json", _compact(long_grid), immutable=True)
    drive.put(month + "/SHORT_GRID.json", _compact(short_grid), immutable=True)

    def pointer_result(summary: dict[str, Any]) -> dict[str, Any]:
        best = summary["best_final_oos"]
        return {
            "status": summary["status"],
            "pass_count": summary["pass_count"],
            "parameter_points": summary["parameter_points"],
            "eligible_full_501": summary["eligible_full_501"],
            "best": {
                "stop_atr": best["stop_atr"],
                "target_r": best["target_r"],
                "max_hold": best["max_hold"],
                **best["final_oos"],
                "stability": best["stability"],
            },
        }

    return {
        "month": month,
        "snapshot": snapshot,
        "as_of": asof,
        "regression": regression,
        "pointer": {
            "hk_month_file": month,
            "hk_snapshot": snapshot,
            "hk_as_of": asof,
            "hk_long_result": pointer_result(long_result),
            "hk_short_result": pointer_result(short_result),
            "hk_short_list_version": manifest["hkex_designated"]["version"],
            "hk_vertical_overlay": HK_VERTICAL_OVERLAY,
        },
    }
