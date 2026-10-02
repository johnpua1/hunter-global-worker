import csv
import gzip
import io
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hunter-global"))
import quarterly_v2 as q


class QuarterlyV2Tests(unittest.TestCase):
    def test_g178_split_ratios_preserve_249_245_and_7_tail(self):
        dates=[f"D{i:03d}" for i in range(501)]
        split=q._dynamic_split(dates)
        self.assertEqual(split["is_end"], "D248")
        self.assertEqual(split["oos_start"], "D249")
        self.assertEqual(split["oos_end"], "D493")
        self.assertEqual(split["unlabelled_tail_sessions"], 7)

    def test_no_answer_fixture_validation_path(self):
        self.assertFalse(hasattr(q, "_g178_fixture"))

    def test_suspect_bar_is_per_bar(self):
        good={"open":10,"high":11,"low":9,"close":10.5,"volume":100}
        bad={"open":10,"high":9.5,"low":9,"close":10.5,"volume":100}
        self.assertFalse(q._bar_suspect(good))
        self.assertTrue(q._bar_suspect(bad))

    def test_legacy_benchmark_csv_gzip_reader(self):
        raw=io.StringIO()
        w=csv.DictWriter(raw,fieldnames=["ticker","date","open","high","low","close","role","candidate_eligible"])
        w.writeheader()
        w.writerow({"ticker":"SPY","date":"2026-10-01","open":"1","high":"2","low":"0.5","close":"1.5",
                    "role":"BENCHMARK","candidate_eligible":"false"})
        rows=q._parse_gz_csv(gzip.compress(raw.getvalue().encode("utf-8")))
        self.assertEqual(rows[0]["ticker"],"SPY")
        self.assertEqual(rows[0]["close"],1.5)


if __name__ == "__main__":
    unittest.main()
