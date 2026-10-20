"""Research-only evaluation adapter: never fetches data, issues predictions or trades.

Consumes a caller-supplied, mature, explicitly sourced snapshot.  All three
libraries are actual imports; their use is kept OUTSIDE the V3 model/issuer.
No network, credentials, database writes, broker APIs, or source mutation.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd


class InvalidEvaluationInput(ValueError):
    """Invalid or non-causal research input; never treat it as a score."""


def _series(values: list[dict], columns: list[str]) -> pd.DataFrame:
    if len(values) < 35:
        raise InvalidEvaluationInput("At least 35 complete observed bars required")
    frame = pd.DataFrame.from_records(values)
    required = ["timestamp", *columns]
    if any(k not in frame for k in required):
        raise InvalidEvaluationInput("Missing required input columns")
    dt = pd.to_datetime(frame["timestamp"], utc=True, errors="raise").dt.tz_localize(None)
    if dt.duplicated().any() or not dt.is_monotonic_increasing:
        raise InvalidEvaluationInput("Non-increasing or duplicate observed timestamps")
    frame.index = pd.DatetimeIndex(dt)
    frame = frame.drop(columns=["timestamp"])
    for k in columns:
        frame[k] = pd.to_numeric(frame[k], errors="raise")
        if not frame[k].map(math.isfinite).all():
            raise InvalidEvaluationInput("Non-finite values")
    return frame


def _risk(returns: pd.Series, periods_per_year: int) -> dict[str, float]:
    import quantstats as qs

    r = returns.dropna().astype(float)
    if len(r) < 20 or not r.map(math.isfinite).all() or (r <= -1.0).any():
        raise InvalidEvaluationInput("Missing valid risk-return series")
    sharpe = float(qs.stats.sharpe(r, rf=0, periods=periods_per_year))
    mdd = float(qs.stats.max_drawdown(r))
    if not all(math.isfinite(v) for v in (sharpe, mdd)):
        raise InvalidEvaluationInput("Risk engine produced invalid output")
    return {
        "sharpe_rf0": round(sharpe, 6),
        "periods_per_year_assumption": periods_per_year,
        "max_drawdown_pct": round(100.0 * mdd, 6),
        "positive_period_pct": round(100.0 * (r > 0).mean(), 4),
    }


def evaluate_qqqm(case: dict[str, Any]) -> dict[str, Any]:
    """Backtesting.py execution check, then QuantStats on strategy equity.

    Signals must be computed entirely from completed historical bars BEFORE
    this function is called. It NEVER infers a live exit from a last green bar.
    """
    from backtesting import Backtest, Strategy

    if case.get("instrument") != "US.QQQM":
        raise InvalidEvaluationInput("QQQM engine only accepts US.QQQM")
    if case.get("exit_contract") != "CANDIDATE_FIRST_RED_CONFIRMED_NEXT_OPEN":
        raise InvalidEvaluationInput("Exit research candidate must be explicitly labeled")
    if case.get("price_basis") != "FORWARD_EXCLUDE_CASH_DIVIDEND":
        raise InvalidEvaluationInput("Mixed or unknown QQQM price adjustment basis")
    columns = ["open", "high", "low", "close", "volume", "entry_signal", "exit_signal"]
    frame = _series(case["bars"], columns)
    if len(frame) < 250:
        raise InvalidEvaluationInput("Insufficient 240-hour warmup plus observed signals")
    frame = frame.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
        "entry_signal": "Enter", "exit_signal": "Exit",
    })
    for k in ("Open", "High", "Low", "Close"):
        if (frame[k] <= 0).any():
            raise InvalidEvaluationInput("OHLC must be positive")
    if ((frame["High"] < frame[["Open", "Close", "Low"]].max(axis=1)) |
            (frame["Low"] > frame[["Open", "Close", "High"]].min(axis=1))).any():
        raise InvalidEvaluationInput("Invalid OHLC high/low range")
    if ((frame["Enter"] > 0) & (frame["Exit"] > 0)).any():
        raise InvalidEvaluationInput("Conflicting entry and exit on the same bar")
    if frame[["Enter", "Exit"]].iloc[:240].to_numpy().any():
        raise InvalidEvaluationInput("Signals in 240-bar warmup")
    if "commission_per_side" not in case:
        raise InvalidEvaluationInput("Commission must be explicitly provided; no guessed default")
    costs = float(case["commission_per_side"])
    if not (0 <= costs < 0.025):
        raise InvalidEvaluationInput("Invalid transaction commission")
    class NextOpenStrategy(Strategy):
        def init(self):
            pass

        def next(self):
            if self.position:
                if self.data.Exit[-1] > 0:
                    self.position.close()
            elif self.data.Enter[-1] > 0:
                self.buy(size=0.95)

    bt = Backtest(frame, NextOpenStrategy, cash=100000,
                  commission=costs, trade_on_close=False,
                  exclusive_orders=True, finalize_trades=False)
    stats = bt.run()
    trades = stats["_trades"]
    equity = stats["_equity_curve"]["Equity"].astype(float)
    eq_returns = equity.pct_change().dropna()
    if (equity <= 0).any():
        raise InvalidEvaluationInput("Invalid strategy equity curve")
    result = {
        "engine": "Backtesting.py",
        "instrument": "US.QQQM",
        "trade_count_closed": int(len(trades)),
        "win_rate_pct": round(100.0 * float(stats["Win Rate [%]"]) / 100, 4)
                        if len(trades) else None,
        "strategy_return_pct": round(float(stats["Return [%]"]), 6),
        "max_drawdown_pct": round(float(stats["Max. Drawdown [%]"]), 6),
        "commission_per_side": costs,
        "candidate_exit": "First confirmed non-green hourly MACD, next-bar open",
        "live_exit_rule_approved": False,
        "risk": _risk(eq_returns, periods_per_year=252 * 7),
    }
    return result


def evaluate_allocation(case: dict[str, Any]) -> dict[str, Any]:
    """bt weights check and QuantStats risk.

    Prices are supplied as *already constructed* single-count total-return
    indices, valued at intended next-session execution timestamps. We DO NOT
    combine dividends with adjusted prices inside this adapter.
    """
    import bt

    if case.get("strategy") != "QQQI_TQQQ_SGOV_ROTATION":
        raise InvalidEvaluationInput("Allocation evaluation restricted to QQQI/TQQQ/SGOV")
    if case.get("price_basis") != "TOTAL_RETURN_SINGLE_COUNT":
        raise InvalidEvaluationInput("Portfolio returns must count dividends once")
    columns = ["qqqi", "tqqq", "sgov", "w_qqqi", "w_tqqq", "w_sgov"]
    frame = _series(case["rows"], columns)
    prices = frame[["qqqi", "tqqq", "sgov"]].rename(columns={
        "qqqi": "QQQI", "tqqq": "TQQQ", "sgov": "SGOV"
    }).astype(float)
    weights = frame[["w_qqqi", "w_tqqq", "w_sgov"]].rename(columns={
        "w_qqqi": "QQQI", "w_tqqq": "TQQQ", "w_sgov": "SGOV"
    }).astype(float)
    if (prices <= 0).any().any():
        raise InvalidEvaluationInput("Invalid total-return prices")
    if (weights < -1e-12).any().any() or (weights > 1 + 1e-12).any().any():
        raise InvalidEvaluationInput("Invalid target allocation weights")
    if ((weights.sum(axis=1) - 1).abs() > 1e-8).any():
        raise InvalidEvaluationInput("Allocation weights do not sum to one")
    if not set(weights.round(10).drop_duplicates().itertuples(index=False, name=None)).issubset({
        (0.8, 0.2, 0.0), (1.0, 0.0, 0.0), (0.5, 0.0, 0.5), (0.0, 0.0, 1.0)
    }):
        raise InvalidEvaluationInput("Weights depart from original video states")
    # Input's prior-close signal timing must be independently checked by
    # the owner; no model training or lookahead-aware signal inference here.
    strategy = bt.Strategy(
        "readonly_video_weights",
        [bt.algos.RunDaily(), bt.algos.WeighTarget(weights), bt.algos.Rebalance()],
    )
    stats = bt.run(bt.Backtest(strategy, prices))
    index = stats.prices.iloc[:, 0].astype(float)
    rets = index.pct_change().dropna()
    if not math.isfinite(float(index.iloc[-1])) or float(index.iloc[-1]) <= 0:
        raise InvalidEvaluationInput("Portfolio backtest returned invalid index")
    return {
        "engine": "bt",
        "portfolio": "QQQI/TQQQ/SGOV",
        "observations": len(index),
        "portfolio_return_pct": round(100 * (index.iloc[-1] / index.iloc[0] - 1), 6),
        "risk": _risk(rets, periods_per_year=252),
        "causal_allocation_prevalidated_by_caller": True,
        "exec_mark": "Supplied single-count total-return index",
    }


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("mode") not in ("QQQM_RESEARCH", "QQQI_RESEARCH", "V3_TOOL_ABLATION"):
        raise InvalidEvaluationInput("Unknown evaluation mode; cannot alter live predictions")
    if payload.get("data_provenance") != "CONFIRMED_MOOMOO":
        raise InvalidEvaluationInput("Research-only data must have confirmed moomoo provenance")
    if payload.get("prospective_outcomes_only") is not True:
        raise InvalidEvaluationInput("Matured outcome and causal event gate missing")
    case = payload.get("case")
    if not isinstance(case, dict):
        raise InvalidEvaluationInput("No evaluation input data")
    if payload["mode"] == "V3_TOOL_ABLATION":
        from v3_ablation_adapter import evaluate_ablation
        result = evaluate_ablation(payload)
    elif payload["mode"] == "QQQM_RESEARCH":
        result = evaluate_qqqm(case)
    else:
        result = evaluate_allocation(case)
    return {
        "result": result, "evaluation_status": "RESEARCH_ONLY",
        "training_weights_changed": False, "new_forecasts_issued": False,
        "trade_permission": "WAIT", "orders_executed": False,
        "original_predictions_mutated": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only evaluation, input JSON; no broker and no DB access")
    parser.add_argument("input", help="Path to approved input JSON snapshot")
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    report = evaluate(data)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
