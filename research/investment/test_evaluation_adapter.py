"""Contract tests for the existing 3 research libraries (offline synthetic inputs).

No real account/order access.  Safe failure on missing or incomplete sources.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from evaluation_adapter import InvalidEvaluationInput, evaluate


def hourly_case() -> dict:
    bars = []
    for i in range(300):
        p = 100 + i * 0.05 + math.sin(i / 10)
        entry = i in (250, 280)
        exit_signal = i in (266, 295)
        bars.append({
            "timestamp": str(__import__("pandas").Timestamp("2026-01-02")
                            + __import__("pandas").Timedelta(hours=i)),
            "open": p,
            "high": p + 0.8,
            "low": p - 0.8,
            "close": p + 0.10,
            "volume": 25000 + i,
            "entry_signal": int(entry),
            "exit_signal": int(exit_signal),
        })
    return {
        "mode": "QQQM_RESEARCH",
        "data_provenance": "CONFIRMED_MOOMOO",
        "prospective_outcomes_only": True,
        "case": {
            "instrument": "US.QQQM",
            "price_basis": "FORWARD_EXCLUDE_CASH_DIVIDEND",
            "exit_contract": "CANDIDATE_FIRST_RED_CONFIRMED_NEXT_OPEN",
            "commission_per_side": .001,
            "bars": bars
        }
    }


def allocation_case() -> dict:
    rows = []
    for i in range(110):
        d = __import__("pandas").Timestamp("2025-06-02") + __import__("pandas").offsets.BDay(i)
        mode = (0.8, 0.2, 0.0) if i < 52 else (1.0, 0.0, 0.0)
        rows.append({
            "timestamp": str(d.date()),
            "qqqi": 100 * (1.00028 ** i),
            "tqqq": 100 * (1.00072 ** i) * (1.0 + .01 * math.sin(i / 7)),
            "sgov": 100 * (1.00012 ** i),
            "w_qqqi": mode[0],
            "w_tqqq": mode[1],
            "w_sgov": mode[2]
        })
    return {
        "mode": "QQQI_RESEARCH",
        "data_provenance": "CONFIRMED_MOOMOO",
        "prospective_outcomes_only": True,
        "case": {
            "strategy": "QQQI_TQQQ_SGOV_ROTATION",
            "price_basis": "TOTAL_RETURN_SINGLE_COUNT",
            "rows": rows
        }
    }


class EvaluationAdapterTests(unittest.TestCase):
    def test_backtesting_and_risk(self):
        r = evaluate(hourly_case())
        assert r["evaluation_status"] == "RESEARCH_ONLY"
        assert r["result"]["engine"] == "Backtesting.py"
        assert r["result"]["trade_count_closed"] == 2
        assert math.isfinite(r["result"]["risk"]["sharpe_rf0"])
        assert r["trade_permission"] == "WAIT"
        assert not r["new_forecasts_issued"]
        print("PASS research evaluation Backtesting.py + QuantStats")

    def test_bt_and_risk(self):
        r = evaluate(allocation_case())
        assert r["result"]["engine"] == "bt"
        assert r["result"]["observations"] >= 100
        assert math.isfinite(r["result"]["risk"]["sharpe_rf0"])
        assert not r["orders_executed"]
        print("PASS research evaluation bt + QuantStats")

    def test_rejects_wrong_source(self):
        c = hourly_case()
        c["data_provenance"] = "UNKNOWN_VENDOR"
        with self.assertRaises(InvalidEvaluationInput):
            evaluate(c)

    def test_rejects_unapproved_exit(self):
        c = hourly_case()
        c["case"]["exit_contract"] = "HINDSIGHT_LAST_GREEN"
        with self.assertRaises(InvalidEvaluationInput):
            evaluate(c)

    def test_rejects_pre_warmup_signal(self):
        c = hourly_case()
        c["case"]["bars"][20]["entry_signal"] = True
        with self.assertRaises(InvalidEvaluationInput):
            evaluate(c)

    def test_rejects_dividend_double_count(self):
        c = allocation_case()
        c["case"]["price_basis"] = "UNKNOWN_ALREADY_DIVIDEND_ADJUSTED"
        with self.assertRaises(InvalidEvaluationInput):
            evaluate(c)

    def test_rejects_new_portfolio_rules(self):
        c = allocation_case()
        c["case"]["rows"][20]["w_qqqi"] = .9
        c["case"]["rows"][20]["w_tqqq"] = .1
        with self.assertRaises(InvalidEvaluationInput):
            evaluate(c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
