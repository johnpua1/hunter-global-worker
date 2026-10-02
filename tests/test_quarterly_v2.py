import datetime as dt
import importlib.util
import pathlib
import sys
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"hunter-global"))
import quarterly_v2 as q


class QuarterlyV2Tests(unittest.TestCase):
    def test_g178_ratio_recut_keeps_tail_buffer(self):
        start=dt.date(2024,1,1)
        dates=[]
        day=start
        while len(dates)<501:
            if day.weekday()<5:
                dates.append(day.isoformat())
            day+=dt.timedelta(days=1)
        split=q._dynamic_split(dates)
        self.assertEqual(split["window_bars"],501)
        self.assertEqual(split["tail_buffer_sessions"],7)
        analysis=dates[:-7]
        self.assertEqual(split["is_start"],analysis[0])
        self.assertEqual(split["is_end"],analysis[248])
        self.assertEqual(split["oos_start"],analysis[249])
        self.assertEqual(split["oos_end"],analysis[-1])
        self.assertEqual(split["tail_start"],dates[-7])
        self.assertEqual(split["tail_end"],dates[-1])

    def test_segment_does_not_leak_prewindow_warmup(self):
        split={"is_start":"2025-01-10","is_end":"2025-06-30",
               "oos_start":"2025-07-01","oos_end":"2025-12-31"}
        self.assertIsNone(q._segment("2025-01-09",split))
        self.assertEqual(q._segment("2025-01-10",split),"IS")
        self.assertEqual(q._segment("2025-07-01",split),"FINAL_OOS")
        self.assertIsNone(q._segment("2026-01-01",split))

    def test_g178_validation_contract(self):
        direction={
            "is_selected":{
                "LONG":{"ticker":"SPY","signal":"S4","H":10,"n":64,"wr":0.8125},
                "SHORT":{"ticker":"IWM","signal":"S3","H":5,"n":11,"wr":0.6363636363636364}},
            "final_oos_standalone":{"LONG":{"pass":False},"SHORT":{"pass":False}},
            "final_online":[],"LONG":"无合格信号","SHORT":"无合格信号"}
        baseline={
            "LONG":{"IS":{"n":5854},"FINAL_OOS":{"n":7410},"verdict":"MARKET_DRIFT_ONLY"},
            "SHORT":{"IS":{"n":7428},"FINAL_OOS":{"n":7611},"verdict":"OOS_ONLY"}}
        result=q._g178_validation(direction,baseline)
        self.assertTrue(result["pass"])
        direction["LONG"]={"ticker":"SPY"}
        self.assertFalse(q._g178_validation(direction,baseline)["pass"])

    def test_no_fixture_short_circuit_remains(self):
        source=(ROOT/"hunter-global"/"quarterly_v2.py").read_text(encoding="utf-8")
        self.assertNotIn("def _g178_fixture",source)
        self.assertNotIn("direction,baseline=_g178_fixture",source)
        self.assertIn("_load_snapshot_groups",source)
        self.assertIn("vertical_baseline(groups,split)",source)

    def test_vertical_p_is_symmetric(self):
        self.assertAlmostEqual(q._vertical_p("LONG",100,105),1.0)
        self.assertAlmostEqual(q._vertical_p("SHORT",100,95),1.0)
        self.assertAlmostEqual(q._vertical_p("LONG",100,102.5),0.5)
        self.assertAlmostEqual(q._vertical_p("SHORT",100,97.5),0.5)


if __name__=="__main__":
    unittest.main()
