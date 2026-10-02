import pathlib
import sys
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"hunter-global"))
import hk_monthly as h


class FakeDrive:
    def __init__(self):
        self.calendar={
            "market":"HK","sessions":[
                {"trade_date":f"2026-01-{i:02d}","session_status":"OPEN"}
                for i in range(1,29)
            ]
        }
    def list(self,path,name=None):
        if path=="HK/MARKET_CALENDAR":
            return [{"name":"calendar.json","mimeType":"application/json"}]
        if path=="HK/DAILY":
            return [{"name":"2026-02-02"},{"name":"2026-02-03"}]
        return []
    def json(self,path):
        if path=="HK/MARKET_CALENDAR/calendar.json":
            return self.calendar
        raise KeyError(path)


class HKMonthlyTests(unittest.TestCase):
    def test_names_are_month_scoped(self):
        self.assertEqual(h._snapshot_name("2026-10-02"),"HK_SNAPSHOT_2026-10")
        self.assertEqual(h._month_name("2026-10-02"),"HK_MONTH_2026-10")

    def test_metrics_never_emit_json_infinity(self):
        result=h._metrics([1.0,2.0,3.0])
        self.assertIsNone(result["pf"])
        self.assertTrue(result["pf_infinite"])

    def test_stability_fails_closed_without_frozen_stock_evaluator(self):
        grid=[
            {"pre_stability_pass":False},
            {"pre_stability_pass":True},
        ]
        h._stability(grid)
        self.assertEqual(grid[0]["stability"],"NOT_EVALUATED_GATE_ALREADY_FAIL")
        self.assertEqual(grid[0]["status"],"FAIL")
        self.assertEqual(grid[1]["stability"],"STABILITY_CONTRACT_REQUIRED")
        self.assertEqual(grid[1]["status"],"FAIL")

    def test_hk_calendar_accepts_session_status_and_completed_daily(self):
        # Use a large synthetic calendar so the 501-session gate is reached.
        d=FakeDrive()
        d.calendar["sessions"]=[
            {"trade_date":f"{2024 + (i//365):04d}-01-{(i%28)+1:02d}",
             "session_status":"OPEN"}
            for i in range(510)
        ]
        dates=h._hk_open_dates(d,"2026-12-31")
        self.assertIn("2026-02-02",dates)
        self.assertIn("2026-02-03",dates)
        self.assertGreaterEqual(len(dates),501)

    def test_validation_contract_is_exactly_zero_of_27(self):
        self.assertEqual(h.VALIDATION_LONG_PASS,0)
        self.assertEqual(h.VALIDATION_SHORT_PASS,0)
        self.assertEqual(len(h.STOPS)*len(h.TARGETS)*len(h.HOLDS),27)


if __name__=="__main__":
    unittest.main()
