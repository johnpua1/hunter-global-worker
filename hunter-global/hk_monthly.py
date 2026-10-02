"""HK monthly STOCK_EDGE recertification.

This module monthly-izes the frozen HK GLOBAL-V1 LONG/SHORT certification
contracts only. It does not import US D1 logic and it never creates a HK
vertical overlay.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
import math
import re
from collections import defaultdict
from typing import Any

import requests

from monthly_v2 import _compact, _ema, _parse_gz_ndjson, _sha, _write_gz_ndjson
from analytics import compose, split_adjust
from derived import read_files

STOPS = (1.0, 1.5, 2.0)
TARGETS = (1.5, 2.0, 3.0)
HOLDS = (5, 10, 20)
WINDOW_BARS = 501
WARMUP = 50
SLIPPAGE = 0.0005
HK_BUY_FEE = 0.0011
HK_SELL_FEE = 0.0011
BROKER_COMMISSION_BUY = 0.0
BROKER_COMMISSION_SELL = 0.0
HK_VERTICAL_OVERLAY = "N/A"

# Frozen GLOBAL-V1 split shape on the original 501-session window:
# pre-research=6, Research=128, Validation=125, Final-OOS=239, tail=3.
SPLIT_SHAPE = (6, 128, 125, 239, 3)


def _month(asof: str) -> str:
    return asof[:7]


def _snapshot_name(asof: str) -> str:
    return "HK_SNAPSHOT_" + _month(asof)


def _month_name(asof: str) -> str:
    return "HK_MONTH_" + _month(asof)


def _open_dates(drive, asof: str) -> list[str]:
    dates = {row["date"] for row in _parse_gz_ndjson(drive.read("HK/CALENDAR_BASE.ndjson.gz"))
             if row.get("date") <= asof}
    try:
        for item in drive.list("HK/DAILY"):
            name = item.get("name", "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name) and name <= asof:
                dates.add(name)
    except RuntimeError:
        pass
    out = sorted(dates)
    if len(out) < WINDOW_BARS:
        raise RuntimeError("HK_MONTH_CALENDAR_LT_501")
    return out[-WINDOW_BARS:]


def _split(open_dates: list[str]) -> dict[str, Any]:
    if len(open_dates) != WINDOW_BARS:
        raise RuntimeError("HK_MONTH_SPLIT_REQUIRES_501_SESSIONS")
    pre, research_n, validation_n, oos_n, tail_n = SPLIT_SHAPE
    if sum(SPLIT_SHAPE) != WINDOW_BARS:
        raise RuntimeError("HK_MONTH_SPLIT_SHAPE_DRIFT")
    research_start = pre
    validation_start = research_start + research_n
    oos_start = validation_start + validation_n
    tail_start = oos_start + oos_n
    return {
        "mode": "GLOBAL_V1_RATIO_RECUT",
        "window_bars": WINDOW_BARS,
        "pre_research_sessions": pre,
        "research_sessions": research_n,
        "validation_sessions": validation_n,
        "final_oos_sessions": oos_n,
        "tail_sessions": tail_n,
        "research_start": open_dates[research_start],
        "research_end": open_dates[validation_start - 1],
        "validation_start": open_dates[validation_start],
        "validation_end": open_dates[oos_start - 1],
        "oos_start": open_dates[oos_start],
        "oos_end": open_dates[tail_start - 1],
        "tail_start": open_dates[tail_start],
        "tail_end": open_dates[-1],
    }


def _segment(day: str, split: dict[str, Any]) -> str:
    if split["research_start"] <= day <= split["research_end"]:
        return "RESEARCH"
    if split["validation_start"] <= day <= split["validation_end"]:
        return "VALIDATION"
    if split["oos_start"] <= day <= split["oos_end"]:
        return "FINAL_OOS"
    return "OUTSIDE"


def _load_hk(drive, asof: str) -> tuple[dict[str, dict], dict[str, list[dict]], list[str]]:
    universe = drive.json("HK/CURRENT_UNIVERSE.json")
    active = {x["security_id"]: x for x in universe["securities"]
              if x.get("listing_status") == "ACTIVE"}
    base_asof = drive.json("HK/CHECKPOINT.json")["as_of"]

    daily = defaultdict(list)
    for item in drive.list("HK/DAILY"):
        name = item.get("name", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            continue
        if name <= base_asof or name > asof:
            continue
        for path in read_files(drive, "HK", "DAILY/" + name, (".ndjson.gz", ".ndjson.gzip")):
            for row in _parse_gz_ndjson(drive.read(path)):
                sid = row.get("security_id")
                if sid in active:
                    daily[sid].append(row)

    patches = defaultdict(list)
    for path in read_files(drive, "HK", "REPAIR_PATCH", ".json"):
        payload = drive.json(path)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = [payload] if isinstance(payload, dict) else []
        for patch in items:
            sid = patch.get("security_id")
            if sid in active and patch.get("accepted") is True:
                patches[sid].append(patch)

    events = []
    for path in read_files(drive, "HK", "CORPORATE_ACTIONS", ".json"):
        payload = drive.json(path)
        if isinstance(payload, list):
            events.extend(payload)

    groups: dict[str, list[dict]] = {}
    base_files = sorted(x["name"] for x in drive.list("HK/BASE")
                        if x["name"].endswith(".ndjson.gz"))
    seen = set()
    for name in base_files:
        by = defaultdict(list)
        for row in _parse_gz_ndjson(drive.read("HK/BASE/" + name)):
            sid = row.get("security_id")
            if sid in active:
                by[sid].append(row)
        for sid in sorted(by):
            rows = split_adjust(compose(by[sid], patches[sid], daily[sid]), events)
            rows = [dict(r, date=r.get("trade_date", r["date"]))
                    for r in rows if r.get("trade_date", r["date"]) <= asof]
            if rows:
                groups[sid] = sorted(rows, key=lambda x: x["date"])
                seen.add(sid)

    for sid in sorted(set(active) - seen):
        rows = split_adjust(compose([], patches[sid], daily[sid]), events)
        rows = [dict(r, date=r.get("trade_date", r["date"]))
                for r in rows if r.get("trade_date", r["date"]) <= asof]
        if rows:
            groups[sid] = sorted(rows, key=lambda x: x["date"])

    return active, groups, _open_dates(drive, asof)


def _build_snapshot(drive, asof: str) -> tuple[str, dict[str, dict], dict[str, list[dict]], list[str]]:
    snapshot = _snapshot_name(asof)
    manifest_path = snapshot + "/MANIFEST.json"
    if drive.file(manifest_path):
        manifest = drive.json(manifest_path)
        if manifest.get("as_of") != asof:
            raise RuntimeError("HK_SNAPSHOT_ASOF_CONFLICT")
        active_doc = drive.json(snapshot + "/HK_ACTIVE_UNIVERSE.json")
        active = {x["security_id"]: x for x in active_doc["securities"]}
        rows = []
        for item in drive.list(snapshot):
            name = item.get("name", "")
            if name.startswith("HK_ACTIVE_OHLC_PART_") and name.endswith(".ndjson.gz"):
                rows.extend(_parse_gz_ndjson(drive.read(snapshot + "/" + name)))
        by = defaultdict(list)
        for row in rows:
            by[row["security_id"]].append(row)
        groups = {sid: sorted(v, key=lambda x: x["date"]) for sid, v in by.items()}
        return snapshot, active, groups, manifest["open_dates_501"]

    active, groups, open_dates = _load_hk(drive, asof)
    drive.folder(snapshot, create=True)
    active_doc = {
        "schema": "HK_MONTHLY_SNAPSHOT_V1",
        "snapshot": snapshot,
        "market": "HK",
        "as_of": asof,
        "active_count": len(active),
        "securities": [active[k] for k in sorted(active)],
    }
    drive.put(snapshot + "/HK_ACTIVE_UNIVERSE.json", _compact(active_doc), immutable=True)

    parts = []
    sids = sorted(groups)
    for part_no, offset in enumerate(range(0, len(sids), 100), 1):
        out = []
        for sid in sids[offset:offset + 100]:
            out.extend(groups[sid])
        payload = _write_gz_ndjson(out)
        name = f"HK_ACTIVE_OHLC_PART_{part_no:04d}.ndjson.gz"
        drive.put(snapshot + "/" + name, payload, "application/x-gzip", immutable=True)
        parts.append({"name": name, "rows": len(out), "bytes": len(payload), "sha256": _sha(payload)})

    manifest = {
        "schema": "HK_MONTHLY_SNAPSHOT_V1",
        "snapshot": snapshot,
        "market": "HK",
        "as_of": asof,
        "semantic": "IMMUTABLE_READ_ONLY_BY_NAME_AND_SHA256",
        "active_count": len(active),
        "parts": parts,
        "open_dates_501": open_dates,
    }
    drive.put(manifest_path, _compact(manifest), immutable=True)
    return snapshot, active, groups, open_dates


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(value)).replace("\xa0", " ").strip()


def _fetch_hkex_shortable(asof: str) -> dict[str, Any]:
    base = dt.date.fromisoformat(asof)
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 Hunter/1.0", "Accept": "text/html,*/*"}
    for back in range(0, 21):
        day = base - dt.timedelta(days=back)
        stamp = day.strftime("%Y%m%d")
        url = "https://www.hkex.com.hk/eng/market/sec_tradinfo/ds" + stamp + ".htm"
        try:
            response = session.get(url, headers=headers, timeout=45)
        except requests.RequestException:
            continue
        if response.status_code != 200 or "Designated Securities" not in response.text:
            continue
        rows = []
        for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", response.text, flags=re.I | re.S):
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, flags=re.I | re.S)
            if len(cells) < 2:
                continue
            code_text = _strip_tags(cells[1])
            if not re.fullmatch(r"\d{1,5}", code_text):
                continue
            code = int(code_text)
            name = _strip_tags(cells[2]) if len(cells) > 2 else ""
            rows.append({"stock_code": code, "ticker": f"{code:04d}.HK", "name": name})
        dedup = {x["stock_code"]: x for x in rows}
        if len(dedup) < 100:
            continue
        effective = day.isoformat()
        return {
            "effective_date": effective,
            "version": "HKEX_DESIGNATED_SHORT_SELLING_" + stamp,
            "url": url,
            "rows": [dedup[k] for k in sorted(dedup)],
        }
    raise RuntimeError("HKEX_DESIGNATED_SHORT_SELLING_NOT_FOUND")


def _archive_hkex(drive, doc: dict[str, Any]) -> str:
    name = doc["version"] + ".csv"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=("stock_code", "ticker", "name"))
    writer.writeheader()
    writer.writerows(doc["rows"])
    drive.put(name, buf.getvalue().encode("utf-8"), "text/plain", immutable=True)
    return name


def _true_range(rows: list[dict]) -> list[float]:
    out = []
    prev = None
    for row in rows:
        h, l, c = float(row["high"]), float(row["low"]), float(row["close"])
        tr = h - l if prev is None else max(h - l, abs(h - prev), abs(l - prev))
        out.append(tr)
        prev = c
    return out


def _atr14(rows: list[dict]) -> list[float | None]:
    tr = _true_range(rows)
    out: list[float | None] = [None] * len(rows)
    if len(rows) < 14:
        return out
    atr = sum(tr[:14]) / 14.0
    out[13] = atr
    for i in range(14, len(rows)):
        atr = (atr * 13.0 + tr[i]) / 14.0
        out[i] = atr
    return out


def _features(rows: list[dict]) -> dict[str, list[Any]]:
    closes = [float(r["close"]) for r in rows]
    e8, e17, e50 = _ema(closes, 8), _ema(closes, 17), _ema(closes, 50)
    macd = [a - b for a, b in zip(e8, e17)]
    sig9 = _ema(macd, 9)
    hist = [a - b for a, b in zip(macd, sig9)]
    long_sig = [False] * len(rows)
    short_sig = [False] * len(rows)
    for i in range(WARMUP, len(rows)):
        long_sig[i] = macd[i] > 0 >= macd[i - 1] and e17[i] > e50[i] and hist[i] > 0
        short_sig[i] = macd[i] < 0 <= macd[i - 1] and e17[i] < e50[i] and hist[i] < 0
    return {"atr": _atr14(rows), "LONG": long_sig, "SHORT": short_sig}


def _fill(price: float, side: str, entering: bool) -> float:
    if side == "LONG":
        return price * (1.0 + SLIPPAGE if entering else 1.0 - SLIPPAGE)
    return price * (1.0 - SLIPPAGE if entering else 1.0 + SLIPPAGE)


def _trade_r(entry: float, exit_price: float, risk: float, side: str, net: bool) -> float:
    gross = ((exit_price - entry) if side == "LONG" else (entry - exit_price)) / risk
    if not net:
        return gross
    if side == "LONG":
        buy, sell = entry, exit_price
    else:
        buy, sell = exit_price, entry
    fee = (buy * (HK_BUY_FEE + BROKER_COMMISSION_BUY) +
           sell * (HK_SELL_FEE + BROKER_COMMISSION_SELL)) / risk
    return gross - fee


def _simulate_profile(rows: list[dict], feat: dict[str, list[Any]], split: dict[str, Any],
                      side: str, stop_atr: float, target_r: float, max_hold: int) -> list[dict]:
    signals = feat[side]
    opposite = feat["SHORT" if side == "LONG" else "LONG"]
    trades = []
    i = WARMUP
    while i < len(rows) - 1:
        if not signals[i] or feat["atr"][i] is None:
            i += 1
            continue
        segment = _segment(rows[i]["date"], split)
        if segment not in ("RESEARCH", "VALIDATION", "FINAL_OOS"):
            i += 1
            continue

        entry_i = i + 1
        raw_open = float(rows[entry_i]["open"])
        entry = _fill(raw_open, side, True)
        risk = stop_atr * float(feat["atr"][i])
        if not (entry > 0 and risk > 0 and math.isfinite(entry) and math.isfinite(risk)):
            i += 1
            continue
        stop = entry - risk if side == "LONG" else entry + risk
        target = entry + target_r * risk if side == "LONG" else entry - target_r * risk
        if target <= 0:
            i += 1
            continue

        exit_i = None
        exit_fill = None
        last_i = min(len(rows) - 1, entry_i + max_hold - 1)
        j = entry_i
        while j <= last_i:
            row = rows[j]
            o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
            if side == "LONG":
                if o <= stop:
                    exit_i, exit_fill = j, _fill(o, side, False)
                    break
                if l <= stop:
                    exit_i, exit_fill = j, _fill(stop, side, False)
                    break
                if h >= target:
                    exit_i, exit_fill = j, _fill(target, side, False)
                    break
            else:
                if o >= stop:
                    exit_i, exit_fill = j, _fill(o, side, False)
                    break
                if h >= stop:
                    exit_i, exit_fill = j, _fill(stop, side, False)
                    break
                if l <= target:
                    exit_i, exit_fill = j, _fill(target, side, False)
                    break

            # Opposite exit is close-confirmed and therefore fills next valid open.
            if opposite[j] and j + 1 < len(rows):
                exit_i, exit_fill = j + 1, _fill(float(rows[j + 1]["open"]), side, False)
                break
            j += 1

        if exit_i is None:
            exit_i = last_i
            exit_fill = _fill(float(rows[exit_i]["close"]), side, False)

        trades.append({
            "security_id": rows[i]["security_id"],
            "signal_date": rows[i]["date"],
            "entry_date": rows[entry_i]["date"],
            "exit_date": rows[exit_i]["date"],
            "segment": segment,
            "r_gross": _trade_r(entry, exit_fill, risk, side, False),
            "r_net": _trade_r(entry, exit_fill, risk, side, True),
        })
        i = max(i + 1, exit_i + 1)
    return trades


def _metrics(trades: list[dict], segment: str, field: str = "r_net") -> dict[str, Any]:
    values = [float(t[field]) for t in trades if t["segment"] == segment and math.isfinite(float(t[field]))]
    wins = [x for x in values if x > 0]
    losses = [x for x in values if x < 0]
    # Keep JSON/Apps-Script transport finite while preserving the mathematical
    # meaning of an all-winning sample for threshold comparisons.
    pf = (sum(wins) / abs(sum(losses))) if losses else (1e308 if wins else None)
    return {
        "n": len(values),
        "wr": (len(wins) / len(values)) if values else None,
        "pf": pf,
        "exp": (sum(values) / len(values)) if values else None,
    }


def _pre_pass(metric: dict[str, Any]) -> tuple[bool, float]:
    wr = metric["wr"]
    required = 0.15 if wr is not None and wr < 0.45 else 0.10
    ok = (metric["n"] >= 50 and metric["pf"] is not None and metric["pf"] >= 1.20 and
          metric["exp"] is not None and metric["exp"] >= required)
    return ok, required


def _apply_stability(rows: list[dict], status_key: str) -> None:
    keyed = {(r["stop_atr"], r["target_r"], r["max_hold"]): r for r in rows}
    pre = {k for k, r in keyed.items() if r["pre_stability_pass"]}
    for key, row in keyed.items():
        if not row["pre_stability_pass"]:
            row["stability"] = "NOT_EVALUATED_GATE_ALREADY_FAIL"
            row[status_key] = "FAIL"
            continue
        s, t, h = key
        neighbors = 0
        for candidate in ((1.0, 0, 0), (-1.0, 0, 0), (0, 0.5, 0), (0, -0.5, 0)):
            ns, nt = s + candidate[0], t + candidate[1]
            nk = (ns, nt, h)
            if nk in pre:
                neighbors += 1
        row["stability"] = "PASS" if neighbors >= 1 else "OVERFIT_RISK"
        row[status_key] = "PASS" if row["stability"] == "PASS" else "FAIL"


def _edge_grid(groups: dict[str, list[dict]], open_dates: list[str], split: dict[str, Any],
               side: str, eligible_sids: set[str]) -> tuple[list[dict], int]:
    calendar = set(open_dates)
    prepared = []
    signal_count = 0
    for sid in sorted(eligible_sids):
        rows = [r for r in groups.get(sid, ()) if r["date"] in calendar]
        by_date = {r["date"]: r for r in rows}
        if len(by_date) != WINDOW_BARS or any(day not in by_date for day in open_dates):
            continue
        rows = [by_date[day] for day in open_dates]
        feat = _features(rows)
        signal_count += sum(1 for x in feat[side] if x)
        prepared.append((rows, feat))

    grid = []
    status_key = "stock_edge_status" if side == "LONG" else "stock_short_edge_status"
    for stop in STOPS:
        for target in TARGETS:
            for hold in HOLDS:
                all_trades = []
                for rows, feat in prepared:
                    all_trades.extend(_simulate_profile(rows, feat, split, side, stop, target, hold))
                val = _metrics(all_trades, "VALIDATION", "r_net")
                oos = _metrics(all_trades, "FINAL_OOS", "r_net")
                gross = _metrics(all_trades, "FINAL_OOS", "r_gross")
                pre, required = _pre_pass(oos)
                fail = []
                if oos["n"] < 50:
                    fail.append("N_LT_50")
                if oos["pf"] is None or oos["pf"] < 1.20:
                    fail.append("PF_LT_1.20")
                if oos["exp"] is None or oos["exp"] < required:
                    fail.append("EXP_LT_" + ("0.15" if required == 0.15 else "0.10"))
                grid.append({
                    "stop_atr": stop,
                    "target_r": target,
                    "max_hold": hold,
                    "validation_n": val["n"],
                    "validation_wr": val["wr"],
                    "validation_pf": val["pf"],
                    "validation_exp": val["exp"],
                    "oos_n": oos["n"],
                    "oos_wr": oos["wr"],
                    "oos_pf": oos["pf"],
                    "oos_exp": oos["exp"],
                    "required_exp": required,
                    "gross_oos_n_before_cost": gross["n"],
                    "gross_oos_wr_before_cost": gross["wr"],
                    "gross_oos_pf_before_cost": gross["pf"],
                    "gross_oos_exp_before_cost": gross["exp"],
                    "hk_fee_buy_rate": HK_BUY_FEE,
                    "hk_fee_sell_rate": HK_SELL_FEE,
                    "broker_commission_buy_rate": BROKER_COMMISSION_BUY,
                    "broker_commission_sell_rate": BROKER_COMMISSION_SELL,
                    "borrow_cost_model": "BORROW_COST_NOT_MODELED" if side == "SHORT" else "N/A",
                    "slippage_bps_per_side": 5,
                    "n_gate": oos["n"] >= 50,
                    "pf_gate": oos["pf"] is not None and oos["pf"] >= 1.20,
                    "exp_gate": oos["exp"] is not None and oos["exp"] >= required,
                    "pre_stability_pass": pre,
                    "stability": None,
                    status_key: None,
                    "high_win_55_label": oos["wr"] is not None and oos["wr"] >= 0.55,
                    "fail_reason": "|".join(fail),
                })
    _apply_stability(grid, status_key)
    return grid, signal_count


def _csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _summary(rows: list[dict], status_key: str) -> dict[str, Any]:
    passes = [r for r in rows if r[status_key] == "PASS"]
    best = max(rows, key=lambda r: (-math.inf if r["oos_exp"] is None else r["oos_exp"]))
    return {
        "pass_count": len(passes),
        "parameter_points": len(rows),
        "status": "CERTIFIED" if passes else "EDGE_NOT_CERTIFIED",
        "best_by_oos_expectancy": {
            "stop_atr": best["stop_atr"],
            "target_r": best["target_r"],
            "max_hold": best["max_hold"],
            "n": best["oos_n"],
            "wr": best["oos_wr"],
            "pf": best["oos_pf"],
            "expectancy_r": best["oos_exp"],
            "stability": best["stability"],
            "status": best[status_key],
        },
    }


def run_hk_monthly(drive, validation: bool = False) -> dict[str, Any]:
    checkpoint = drive.json("HK/CONTROL/DAILY_CHECKPOINT.json")
    asof = checkpoint.get("last_completed_date") or checkpoint.get("as_of")
    if not asof:
        raise RuntimeError("HK_DAILY_CHECKPOINT_DATE_MISSING")

    month_dir = _month_name(asof)
    result_path = month_dir + "/RESULT.json"
    if drive.file(result_path):
        result = drive.json(result_path)
        if validation:
            if result["long"]["pass_count"] != 0 or result["short"]["pass_count"] != 0:
                raise RuntimeError("HK_G160_G161_REGRESSION_MISMATCH")
        return result

    snapshot, active, groups, open_dates = _build_snapshot(drive, asof)
    split = _split(open_dates)
    calendar = set(open_dates)
    long_eligible = set(active)
    long_full = {sid for sid in long_eligible
                 if len({r["date"] for r in groups.get(sid, ()) if r["date"] in calendar}) == WINDOW_BARS}

    hkex = _fetch_hkex_shortable(asof)
    hkex_archive = _archive_hkex(drive, hkex)
    shortable_tickers = {x["ticker"] for x in hkex["rows"]}
    short_eligible = {sid for sid, sec in active.items() if sec.get("ticker") in shortable_tickers}
    short_full = {sid for sid in short_eligible
                  if len({r["date"] for r in groups.get(sid, ()) if r["date"] in calendar}) == WINDOW_BARS}

    long_grid, long_signals = _edge_grid(groups, open_dates, split, "LONG", long_full)
    short_grid, short_signals = _edge_grid(groups, open_dates, split, "SHORT", short_full)
    long_summary = _summary(long_grid, "stock_edge_status")
    short_summary = _summary(short_grid, "stock_short_edge_status")

    if validation and (long_summary["pass_count"] != 0 or short_summary["pass_count"] != 0):
        raise RuntimeError("HK_G160_G161_REGRESSION_MISMATCH:" + json.dumps(
            {"long": long_summary["pass_count"], "short": short_summary["pass_count"]},
            sort_keys=True))

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    result = {
        "schema": "HK_MONTHLY_STOCK_EDGE_V1",
        "month": month_dir,
        "snapshot": snapshot,
        "as_of": asof,
        "created_at_myt": now,
        "split": split,
        "active_count": len(active),
        "long_full_501": len(long_full),
        "shortable_list_version": hkex["version"],
        "shortable_effective_date": hkex["effective_date"],
        "shortable_official_url": hkex["url"],
        "shortable_archive": hkex_archive,
        "shortable_active_intersection": len(short_eligible),
        "short_full_501": len(short_full),
        "long_signal_count": long_signals,
        "short_signal_count": short_signals,
        "long": long_summary,
        "short": short_summary,
        "hk_vertical_overlay": HK_VERTICAL_OVERLAY,
        "cost_model": {
            "slippage_bps_per_side": 5,
            "hk_buy_rate": HK_BUY_FEE,
            "hk_sell_rate": HK_SELL_FEE,
            "broker_commission_buy_rate": 0.0,
            "broker_commission_sell_rate": 0.0,
            "borrow_cost_model": "BORROW_COST_NOT_MODELED",
        },
        "validation": {
            "requested": validation,
            "expected": {"long_pass": 0, "short_pass": 0},
            "pass": (long_summary["pass_count"] == 0 and short_summary["pass_count"] == 0),
        },
    }

    drive.folder(month_dir, create=True)
    drive.put(month_dir + "/LONG_EDGE.csv", _csv_bytes(long_grid), "text/plain", immutable=True)
    drive.put(month_dir + "/SHORT_EDGE.csv", _csv_bytes(short_grid), "text/plain", immutable=True)
    drive.put(result_path, _compact(result), immutable=True)
    return result
