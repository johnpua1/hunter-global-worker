import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from runner import session_closed


class MarketCloseTests(unittest.TestCase):
    def test_hong_kong_same_day_close(self):
        day = dt.date(2026, 9, 28)
        before = dt.datetime(2026, 9, 28, 9, 29, tzinfo=dt.timezone.utc)
        after = before + dt.timedelta(minutes=1)
        self.assertFalse(session_closed(day, before.astimezone(ZoneInfo("Asia/Hong_Kong"))))
        self.assertTrue(session_closed(day, after.astimezone(ZoneInfo("Asia/Hong_Kong"))))

    def test_new_york_dst_close(self):
        day = dt.date(2026, 9, 28)
        close = dt.datetime(2026, 9, 28, 21, 30, tzinfo=dt.timezone.utc)
        self.assertTrue(session_closed(day, close.astimezone(ZoneInfo("America/New_York"))))
        self.assertFalse(session_closed(day, (close - dt.timedelta(minutes=1)).astimezone(
            ZoneInfo("America/New_York"))))
        winter_day = dt.date(2026, 12, 28)
        winter_close = dt.datetime(2026, 12, 28, 22, 30, tzinfo=dt.timezone.utc)
        self.assertTrue(session_closed(winter_day, winter_close.astimezone(
            ZoneInfo("America/New_York"))))


if __name__ == "__main__":
    unittest.main()
