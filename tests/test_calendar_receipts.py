import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_foundation import MemoryDrive
from market_calendar import materialize
from runner import compact


class CalendarReceiptTests(unittest.TestCase):
    def run_calendar(self, capped=False):
        drive = MemoryDrive()
        path = "US/CONTROL/DAILY_RUN_2026-10-02.json"
        drive.data[path] = compact({"status": "MARKET_WIDE_DATA_UNAVAILABLE"})
        base = SimpleNamespace(calendar=["2024-10-01", "2026-10-02"])
        with patch("market_calendar.load_market", return_value=base), \
                patch.object(drive, "file", wraps=drive.file) as exists, \
                patch.object(drive, "list", wraps=drive.list) as listing:
            if capped == "error":
                listing.side_effect = RuntimeError("BRIDGE_LIST_LIMIT")
            elif capped:
                listing.return_value = [{"name": f"other-{i}"} for i in range(1000)]
            materialize(drive, "US", "2026-10-02")
            receipt_probes = [c for c in exists.call_args_list if "/CONTROL/DAILY_RUN_" in c.args[0]]
            self.assertEqual(len(receipt_probes), 732 if capped else 0)
            listing.assert_called_once_with("US/CONTROL")
        doc = drive.json("US/MARKET_CALENDAR/as_of_2026-10-02.json")
        self.assertEqual(doc["sessions"][-1]["session_status"], "MARKET_WIDE_DATA_UNAVAILABLE")
        self.assertIn("2024-10-02", doc["unverified_weekdays"])

    def test_historical_calendar_uses_one_listing_and_preserves_outage(self):
        self.run_calendar()

    def test_capped_listing_keeps_per_date_existence_checks(self):
        self.run_calendar(capped=True)

    def test_bridge_listing_limit_keeps_per_date_existence_checks(self):
        self.run_calendar(capped="error")


if __name__ == "__main__":
    unittest.main()
