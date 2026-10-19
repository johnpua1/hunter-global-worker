"""Offline installation and execution smoke test; NEVER submits real orders.

Uses deterministic generated OHLCV, not real user holdings or market data.
Real strategy backtests remain separate from this installation verification.
"""
from __future__ import annotations

import importlib.metadata
import math
import sys

import numpy as np
import pandas as pd


def fixture(n: int = 300) -> pd.DataFrame:
    days = pd.bdate_range("2025-01-02", periods=n)
    t = np.arange(n, dtype=float)
    base = 100.0 + 0.06 * t + 1.7 * np.sin(t / 11.0)
    opened = base + 0.18 * np.sin(t / 3.0)
    closed = base + 0.25 * np.cos(t / 5.0)
    return pd.DataFrame(
        {
            "Open": opened,
            "High": np.maximum(opened, closed) + 0.4,
            "Low": np.minimum(opened, closed) - 0.4,
            "Close": closed,
            "Volume": np.full(n, 100_000, dtype=int),
        },
        index=days,
    )


def verify_backtesting(frame: pd.DataFrame) -> dict:
    from backtesting import Backtest, Strategy

    class OneLong(Strategy):
        def init(self):
            pass

        def next(self):
            if not self.position:
                self.buy()

    runner = Backtest(
        frame,
        OneLong,
        cash=10_000,
        commission=0.001,
        exclusive_orders=True,
        trade_on_close=False,
        finalize_trades=True,
    )
    stats = runner.run()
    if stats["_trades"].empty:
        raise AssertionError("backtesting.py did not create a completed trade")
    performance = float(stats["Return [%]"])
    if not math.isfinite(performance):
        raise AssertionError("backtesting.py produced nonfinite return")
    return {"name": "backtesting.py", "trades": int(len(stats["_trades"])), "return_pct": round(performance, 4)}


def verify_bt(frame: pd.DataFrame) -> dict:
    import bt

    close = frame["Close"].astype(float)
    prices = pd.DataFrame(
        {"ETF_A": close, "ETF_B": close * (1.0 + 0.001 * np.sin(np.arange(len(close))))},
        index=frame.index,
    )
    portfolio = bt.Strategy(
        "research_equal_weight",
        [
            bt.algos.RunOnce(),
            bt.algos.SelectAll(),
            bt.algos.WeighEqually(),
            bt.algos.Rebalance(),
        ],
    )
    result = bt.run(bt.Backtest(portfolio, prices))
    ending_value = float(result.prices.iloc[-1, 0])
    if not math.isfinite(ending_value) or ending_value <= 0:
        raise AssertionError("bt produced invalid portfolio value")
    return {"name": "bt", "ending_index": round(ending_value, 4)}


def verify_quantstats(frame: pd.DataFrame) -> dict:
    import quantstats as qs

    returns = frame["Close"].pct_change().dropna()
    sharpe = float(qs.stats.sharpe(returns))
    drawdown = float(qs.stats.max_drawdown(returns))
    if not math.isfinite(sharpe) or not math.isfinite(drawdown):
        raise AssertionError("QuantStats produced nonfinite risk metrics")
    if drawdown > 1e-8:
        raise AssertionError("QuantStats max drawdown cannot be positive")
    return {"name": "quantstats", "sharpe": round(sharpe, 4), "max_drawdown": round(drawdown, 6)}


def main() -> None:
    print("INVESTMENT_RESEARCH_INSTALL_SMOKE")
    print("Python", sys.version.split()[0])
    for dist in ("backtesting", "bt", "quantstats"):
        print(f"INSTALLED {dist} {importlib.metadata.version(dist)}")
    frame = fixture()
    for verify in (verify_backtesting, verify_bt, verify_quantstats):
        print("PASS", verify(frame))
    print("PASS ALL THREE RESEARCH PACKAGES")
    print("NO PRODUCTION RUNTIME, BROKER, DATABASE OR ORDER EXECUTION")


if __name__ == "__main__":
    main()
