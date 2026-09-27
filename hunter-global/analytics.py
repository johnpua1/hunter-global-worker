"""Query composition and reproducible, underlying-only derived indicators."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

from runner import compact, parse_lines_gz, lines_gz


def compose(base: list[dict], patches: list[dict], daily: list[dict]) -> list[dict]:
    rows = {(r["security_id"], r.get("trade_date", r["date"])): dict(r) for r in base}
    base_keys = set(rows)
    repaired = set()
    for patch in patches:
        if patch.get("accepted") is True and patch.get("result") in ("RESOLVED", "IDENTITY_FIXED"):
            for row in patch.get("rows", []):
                key = row["security_id"], row.get("trade_date", row["date"])
                rows[key] = dict(row)
                repaired.add(key)
    for row in daily:
        key = row["security_id"], row.get("trade_date", row["date"])
        if key in base_keys:
            raise ValueError("BASE_DAILY_OVERLAP:" + str(key))
        if key not in repaired:
            rows[key] = dict(row)
    return sorted(rows.values(), key=lambda r: (r["security_id"], r.get("trade_date", r["date"])))


def split_adjust(rows: list[dict], events: list[dict]) -> list[dict]:
    # BASE rows are normalized at adjustment_as_of; apply only later events.
    # DAILY rows may carry a newer anchor. Never adjust the same split twice.
    events = list({(e["security_id"], e["effective_date"], e["factor"]): e
                   for e in events}.values())
    result = []
    for item in rows:
        row = dict(item)
        day = row.get("trade_date", row["date"])
        anchor = row["adjustment_as_of"]
        for event in events:
            if (event["security_id"] == row["security_id"] and
                    day < event["effective_date"] and anchor < event["effective_date"]):
                factor = float(event["factor"])
                if factor <= 0:
                    raise ValueError("BAD_SPLIT_FACTOR")
                for key in ("open", "high", "low", "close"):
                    row[key] /= factor
                row["volume"] *= factor
        result.append(row)
    return result


def ema(values: list[float], period: int) -> list[float | None]:
    result = [None] * len(values)
    if len(values) < period:
        return result
    result[period - 1] = statistics.mean(values[:period])
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        result[index] = alpha * values[index] + (1 - alpha) * result[index - 1]
    return result


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = sum(max(0, x) for x in changes[:period]) / period
    losses = sum(max(0, -x) for x in changes[:period]) / period
    for change in changes[period:]:
        gains = (gains * (period - 1) + max(0, change)) / period
        losses = (losses * (period - 1) + max(0, -change)) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100 - 100 / (1 + gains / losses)


def indicators(rows: list[dict], benchmark: list[dict] | None = None) -> dict:
    if not rows:
        raise ValueError("NO_BARS")
    rows = sorted(rows, key=lambda r: r.get("trade_date", r["date"]))
    close = [float(r["close"]) for r in rows]
    low = [float(r["low"]) for r in rows]
    volume = [float(r["volume"]) for r in rows]
    last = close[-1]
    ma = {n: statistics.mean(close[-n:]) if len(close) >= n else None
          for n in (20, 50, 200)}
    slope = {n: (ma[n] / statistics.mean(close[-n-5:-5]) - 1)
             if len(close) >= n + 5 else None for n in ma}
    low_distance = {n: last / min(low[-n:]) - 1 if len(low) >= n else None
                    for n in (20, 60, 120, 252)}
    high52 = max(float(r["high"]) for r in rows[-252:])
    low52 = min(float(r["low"]) for r in rows[-252:])
    e12, e26 = ema(close, 12), ema(close, 26)
    macd_values = [a - b for a, b in zip(e12, e26) if a is not None and b is not None]
    macd = macd_values[-1] if macd_values else None
    signal = ema(macd_values, 9)[-1] if len(macd_values) >= 9 else None
    # Both prior closes must be strictly above their predecessors' running
    # lows, and the current close must have reclaimed the 20-day MA.
    no_new_low = (len(close) >= 3 and close[-2] >= min(close[:-2]) and
                  close[-1] >= min(close[:-1]))
    bottom_confirmed = bool(no_new_low and ma[20] is not None and last > ma[20])
    low_zone = low_distance[60] is not None and low_distance[60] <= .10
    typical_dollar_volume = [float(r["close"]) * v for r, v in zip(rows, volume)]
    def change(n, series):
        return series[-1] / series[-n-1] - 1 if len(series) > n and series[-n-1] else None
    relative = None
    if benchmark:
        bm = {r.get("trade_date", r["date"]): float(r["close"]) for r in benchmark}
        aligned = [(r, bm[r.get("trade_date", r["date"])]) for r in rows
                   if r.get("trade_date", r["date"]) in bm]
        if len(aligned) > 20:
            relative = change(20, [float(r["close"]) for r, _ in aligned]) - change(
                20, [v for _, v in aligned])
    return {
        "security_id": rows[-1]["security_id"], "trade_date": rows[-1].get("trade_date", rows[-1]["date"]),
        "close": last, "ma": ma, "slope_5d": slope,
        "alignment": "BULL" if all(ma.values()) and ma[20] > ma[50] > ma[200] else
                     "BEAR" if all(ma.values()) and ma[20] < ma[50] < ma[200] else "MIXED_OR_SHORT",
        "price_vs_ma": {n: last / value - 1 if value else None for n, value in ma.items()},
        "low_distance": low_distance, "percentile_52w":
            (last - low52) / (high52 - low52) if high52 > low52 else None,
        "drawdown_52w": last / high52 - 1 if high52 else None,
        "bottom_structure": "STAGE_LOW" if low_zone else "NONE",
        "bottom_confirmation": bottom_confirmed,
        "bottom_label": "阶段低位，收盘确认" if low_zone and bottom_confirmed else
                        "阶段低位，未确认" if low_zone else "非阶段低位",
        "final_bottom": "UNKNOWABLE_AT_THIS_DATE", "buy_signal": False,
        "volume_20d": statistics.mean(volume[-20:]) if len(volume) >= 20 else None,
        "dollar_volume_20d": statistics.mean(typical_dollar_volume[-20:])
                             if len(rows) >= 20 else None,
        "rsi14": rsi(close), "macd": macd,
        "macd_signal": signal, "macd_histogram": macd - signal if signal is not None else None,
        "relative_strength_20d": relative, "rank_score": None,
        "return_20d": change(20, close), "return_60d": change(60, close),
        "return_252d": change(252, close),
    }


def excursions(rows: list[dict], *, signal_date: str | None = None,
               entry_date: str | None = None, entry_price: float | None = None,
               direction: str = "LONG") -> dict:
    anchor = entry_date or signal_date
    if not anchor:
        raise ValueError("ANCHOR_REQUIRED")
    ordered = sorted(rows, key=lambda r: r.get("trade_date", r["date"]))
    index = next((i for i, r in enumerate(ordered)
                  if r.get("trade_date", r["date"]) == anchor), None)
    if index is None:
        raise ValueError("ANCHOR_BAR_MISSING")
    price = float(entry_price if entry_price is not None else ordered[index]["close"])
    if price <= 0:
        raise ValueError("BAD_ENTRY_PRICE")
    if direction not in ("LONG", "SHORT"):
        raise ValueError("DIRECTION_REQUIRED")
    result = {"anchor_date": anchor, "entry_price": price,
              "instrument": "UNDERLYING", "direction": direction}
    for horizon in (5, 10, 20, 60):
        future = ordered[index + 1:index + horizon + 1]
        if len(future) < horizon:
            result[f"{horizon}D"] = None
            continue
        minimum = min(range(horizon), key=lambda i: future[i]["low"])
        maximum = max(range(horizon), key=lambda i: future[i]["high"])
        if direction == "LONG":
            mae = float(future[minimum]["low"]) / price - 1
            mfe = float(future[maximum]["high"]) / price - 1
            adverse_day, favorable_day = minimum + 1, maximum + 1
            final_return = float(future[-1]["close"]) / price - 1
        else:
            mae = 1 - float(future[maximum]["high"]) / price
            mfe = 1 - float(future[minimum]["low"]) / price
            adverse_day, favorable_day = maximum + 1, minimum + 1
            final_return = 1 - float(future[-1]["close"]) / price
        result[f"{horizon}D"] = {
            "MAE": mae, "MFE": mfe, "days_to_MAE": adverse_day,
            "days_to_MFE": favorable_day, "final_return": final_return,
            "MFE_capture_ratio": final_return / mfe if mfe > 0 else None}
    return result
