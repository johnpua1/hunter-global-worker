# Isolated Investment Research Toolchain

**Not part of Hunter production or trading.** This additive research-only folder
contains three Python research packages, with pinned versions and an offline
smoke test. It makes no changes to `Dockerfile`, `hunter-global/requirements.txt`,
existing cloud-run jobs, prediction models, databases, scheduled tasks, or orders.

| Package | Version | Intended research use |
| --- | --- | --- |
| Backtesting.py | 0.6.6 | Executable next-bar fills, commissions, P&L, stop/exit testing |
| bt | 1.2.3 | QQQI/TQQQ/SGOV state rotation and portfolio rebalancing |
| QuantStats | 0.0.86 | Risk metrics, Sharpe, drawdown, return breakdown |

## Independent installation (Python 3.12)

```bash
python3.12 -m venv .venv-investment
. .venv-investment/bin/activate
python -m pip install -r research/investment/requirements.txt
python research/investment/smoke_test.py
```

GitHub Actions workflow `.github/workflows/investment-research-smoke.yml`
does this in a disposable Python 3.12 environment, triggered on the
`research/investment-toolchain-20261019` branch. It does not deploy.

The smoke test uses deterministic, fabricated OHLCV **only** to confirm that
the actual packages install, import, and execute their core functions. This
does **not** claim QQQM/QQQI profitability or validate vendor data. Real
market backtests must reuse existing approved read-only market data, separate
the QQQM long-entry and QQQI savings strategies, avoid lookahead,
model commissions/slippage, and count QQQI/TQQQ/SGOV cash distributions
exactly once. Do not activate live trading.

The Backtesting.py library carries AGPL-3.0 licensing; keep it as a
standalone research tool unless its licensing implications are reviewed.
