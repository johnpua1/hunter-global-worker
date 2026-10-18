"""Fail-closed regression tests for US Phase2 provider-day resumability."""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
import phase2_runtime as p2
from test_foundation import MemoryDrive

SID = "US-000001"
CACHE = "US/PHASE2/EARNINGS_CALENDAR_PROVIDER_CACHE_V1.json"
TODAY = dt.date(2026, 10, 18)
DAYS = [dt.date(2026, 10, d) for d in (19, 20, 21)]


def sample(day):
    return [{"symbol": "TEST", "time": "time-after-hours",
             "epsForecast": "1.23", "reportDate": day}]


class USCalendarResumableTests(unittest.TestCase):
    def setUp(self):
        self.drive = MemoryDrive()
        self.active = {SID: {"security_id": SID, "ticker": "TEST"}}
        self.index = {"TEST": {SID}}

    def test_partial_provider_failure_persists_successes_without_completed_receipt(self):
        calls = []
        def fetch(day):
            calls.append(day)
            if day == str(DAYS[2]):
                raise RuntimeError("HTTP_503")
            return sample(day)
        with patch.object(p2, "future_us_days", return_value=DAYS), \
             patch.object(p2, "nasdaq_rows", side_effect=fetch):
            actual = p2.us_calendar(self.active, self.index, TODAY,
                                    drive=self.drive)
        self.assertEqual(calls, [str(d) for d in DAYS])
        self.assertFalse(actual["calendar_provider_complete"])
        self.assertEqual(len(actual["source_errors"]), 1)
        self.assertEqual(len(actual["events"]), 2)
        self.assertTrue(self.drive.file(CACHE))
        stored = self.drive.json(CACHE)
        self.assertEqual(set(stored["days"]), {str(DAYS[0]), str(DAYS[1])})
        self.assertTrue(all(x["status"] == "SOURCE_OK"
                            for x in stored["days"].values()))
        # A valid cache is evidence for dates, never a forged checkpoint.
        self.assertNotIn("US/CONTROL/DAILY_CHECKPOINT.json", self.drive.writes)

    def test_retry_fetches_only_failed_date_and_finishes_real_source_coverage(self):
        with patch.object(p2, "future_us_days", return_value=DAYS), \
             patch.object(p2, "nasdaq_rows",
                          side_effect=lambda day: (
                              (_ for _ in ()).throw(RuntimeError("HTTP_503"))
                              if day == str(DAYS[-1]) else sample(day))):
            p2.us_calendar(self.active, self.index, TODAY, drive=self.drive)
        called = []
        def fetch(day):
            called.append(day)
            return sample(day)
        with patch.object(p2, "future_us_days", return_value=DAYS), \
             patch.object(p2, "nasdaq_rows", side_effect=fetch):
            actual = p2.us_calendar(self.active, self.index, TODAY,
                                    drive=self.drive)
        self.assertEqual(called, [str(DAYS[-1])])
        self.assertTrue(actual["calendar_provider_complete"])
        self.assertEqual(actual["source_errors"], [])
        self.assertEqual(actual["calendar_provider_fetched_days"], 1)
        self.assertEqual(actual["calendar_provider_reused_verified_days"], 2)
        self.assertEqual(len(actual["events"]), 3)
        self.assertEqual(len(self.drive.json(CACHE)["days"]), 3)

    def test_cache_refreshes_each_new_business_day_for_near_term(self):
        with patch.object(p2, "future_us_days", return_value=DAYS), \
             patch.object(p2, "nasdaq_rows", side_effect=sample):
            p2.us_calendar(self.active, self.index, TODAY, drive=self.drive)
        called = []
        with patch.object(p2, "future_us_days", return_value=DAYS), \
             patch.object(p2, "nasdaq_rows",
                          side_effect=lambda day: (called.append(day), sample(day))[1]):
            actual = p2.us_calendar(self.active, self.index,
                                    TODAY + dt.timedelta(days=1), drive=self.drive)
        self.assertEqual(called, [str(d) for d in DAYS])
        self.assertTrue(actual["calendar_provider_complete"])

    def test_stop_after_three_consecutive_source_failures_never_claims_complete(self):
        all_dates = [TODAY + dt.timedelta(days=n) for n in range(1, 7)]
        calls = []
        def down(day):
            calls.append(day)
            raise ConnectionError("blocked")
        with patch.object(p2, "future_us_days", return_value=all_dates), \
             patch.object(p2, "nasdaq_rows", side_effect=down):
            actual = p2.us_calendar(self.active, self.index, TODAY,
                                    drive=self.drive)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(actual["source_errors"]), 3)
        self.assertFalse(actual["calendar_provider_complete"])
        self.assertFalse(self.drive.file(CACHE))
        self.assertFalse(actual["events"])


if __name__ == "__main__":
    unittest.main()
