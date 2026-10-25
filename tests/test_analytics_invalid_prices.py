"""Regression tests for the production 2026-10-25 zero-low rank crash.

An invalid source bar stays preserved for audit, but never creates a
fabricated low-distance, bottom entry or eligible ranking.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from analytics import indicators


def bars(count=260):
    day = dt.date(2025, 1, 1)
    result = []
    for i in range(count):
        date = (day + dt.timedelta(days=i)).isoformat()
        result.append({
            "date": date, "trade_date": date, "security_id": "US-000001",
            "open": 100., "high": 101., "low": 99., "close": 100.,
            "volume": 1000.,
        })
    return result


class ZeroLowProductionRegressionTests(unittest.TestCase):
    def test_valid_price_history_unchanged(self):
        out = indicators(bars())
        self.assertTrue(out["price_data_valid"])
        self.assertAlmostEqual(out["low_distance"][20], 100 / 99 - 1)

    def test_zero_low_does_not_divide_or_fabricate_bottom(self):
        history = bars()
        history[-5]["low"] = 0
        out = indicators(history)
        self.assertFalse(out["price_data_valid"])
        self.assertIsNone(out["low_distance"][20])
        self.assertIsNone(out["low_distance"][252])
        self.assertIsNone(out["percentile_52w"])
        self.assertIsNone(out["drawdown_52w"])
        self.assertFalse(out["bottom_confirmation"])
        self.assertEqual(history[-5]["low"], 0, "Original evidence must remain unchanged")

    def test_negative_low_rejected_without_exception(self):
        history = bars()
        history[-3]["low"] = -0.5
        out = indicators(history)
        self.assertFalse(out["price_data_valid"])
        self.assertIsNone(out["low_distance"][20])
        self.assertFalse(out["bottom_confirmation"])

    def test_zero_prior_ma_does_not_crash_slope(self):
        history = bars(25)
        for item in history:
            item["close"] = 0.
        history[-1]["close"] = 1.
        out = indicators(history)
        self.assertIsNone(out["slope_5d"][20])
        self.assertFalse(out["price_data_valid"])

    def test_zero_benchmark_reference_returns_unavailable_relative(self):
        history = bars()
        benchmark = bars()
        benchmark[-21]["close"] = 0.
        result = indicators(history, benchmark)
        self.assertIsNone(result["relative_strength_20d"])


if __name__ == "__main__":
    unittest.main()
