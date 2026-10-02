import datetime as dt
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hunter-global"))
import hk_monthly as h


class HKMonthlyTests(unittest.TestCase):
    def test_split_shape_is_frozen_501_contract(self):
        start = dt.date(2024, 1, 1)
        dates = []
        day = start
        while len(dates) < 501:
            if day.weekday() < 5:
                dates.append(day.isoformat())
            day += dt.timedelta(days=1)
        split = h._split(dates)
        self.assertEqual(split["pre_research_sessions"], 6)
        self.assertEqual(split["research_sessions"], 128)
        self.assertEqual(split["validation_sessions"], 125)
        self.assertEqual(split["final_oos_sessions"], 239)
        self.assertEqual(split["tail_sessions"], 3)
        self.assertEqual(split["research_start"], dates[6])
        self.assertEqual(split["validation_start"], dates[134])
        self.assertEqual(split["oos_start"], dates[259])
        self.assertEqual(split["oos_end"], dates[497])
        self.assertEqual(split["tail_start"], dates[498])

    def test_gate_raises_expectancy_requirement_when_wr_below_45(self):
        ok, required = h._pre_pass({"n": 50, "pf": 1.3, "exp": 0.149, "wr": 0.44})
        self.assertFalse(ok)
        self.assertEqual(required, 0.15)
        ok, required = h._pre_pass({"n": 50, "pf": 1.3, "exp": 0.10, "wr": 0.45})
        self.assertTrue(ok)
        self.assertEqual(required, 0.10)

    def test_hk_vertical_is_permanently_na(self):
        self.assertEqual(h.HK_VERTICAL_OVERLAY, "N/A")

    def test_summary_reports_edge_not_certified_for_zero_pass(self):
        rows = [
            {"stop_atr": 1.0, "target_r": 1.5, "max_hold": 5, "oos_n": 100,
             "oos_wr": 0.4, "oos_pf": 0.9, "oos_exp": -0.1,
             "stability": "NOT_EVALUATED_GATE_ALREADY_FAIL",
             "stock_edge_status": "FAIL"},
            {"stop_atr": 2.0, "target_r": 3.0, "max_hold": 20, "oos_n": 100,
             "oos_wr": 0.5, "oos_pf": 1.1, "oos_exp": 0.05,
             "stability": "NOT_EVALUATED_GATE_ALREADY_FAIL",
             "stock_edge_status": "FAIL"},
        ]
        out = h._summary(rows, "stock_edge_status")
        self.assertEqual(out["pass_count"], 0)
        self.assertEqual(out["status"], "EDGE_NOT_CERTIFIED")
        self.assertEqual(out["best_by_oos_expectancy"]["stop_atr"], 2.0)


if __name__ == "__main__":
    unittest.main()
