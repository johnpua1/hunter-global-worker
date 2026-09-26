import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from analytics import compose, excursions, indicators, split_adjust
from foundation import append_daily_date
from options import current_status
from runner import compact, parse_lines_gz


class MemoryDrive:
    def __init__(self):
        self.data = {"REPAIR_QUEUE.json": compact({"items": []})}
        self.writes = []

    def read(self, path):
        return self.data[path]

    def json(self, path):
        return json.loads(self.read(path))

    def file(self, path):
        return path in self.data

    def list(self, folder):
        prefix = folder + "/"
        return [{"name": path[len(prefix):]} for path in self.data if path.startswith(prefix)]

    def put(self, path, data, **kwargs):
        self.data[path] = data
        self.writes.append(path)

    def append(self, path, data, mime=None):
        if path in self.data:
            raise RuntimeError("APPEND_CONFLICT")
        self.data[path] = data
        self.writes.append(path)


def row(date, close, sid="US-000001", anchor="2026-01-01"):
    return {"security_id": sid, "date": date, "trade_date": date,
            "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": 100, "adjustment_as_of": anchor}


class FoundationTests(unittest.TestCase):
    def test_options_status_stales_after_missed_monthly_refresh(self):
        self.assertEqual(current_status({"status": "TRUE", "checked_at_myt": "2026-08-01T09:00:00+08:00"},
                                        "2026-09"), "STALE")
    def test_patch_composition_and_split_applied_once(self):
        base = [row("2026-01-01", 100)]
        patch = [{"accepted": True, "result": "RESOLVED", "rows": [row("2026-01-01", 110)]}]
        result = compose(base, patch, [row("2026-01-02", 55, anchor="2026-01-02")])
        adjusted = split_adjust(result, [{"security_id": "US-000001",
                                           "effective_date": "2026-01-02", "factor": 2}])
        self.assertEqual([r["close"] for r in adjusted], [55, 55])
        self.assertEqual(adjusted[0]["volume"], 200)

    def test_underlying_excursions_use_future_days(self):
        rows = [row((dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat(), 100 + i)
                for i in range(61)]
        result = excursions(rows, signal_date="2026-01-01")
        self.assertEqual(result["5D"]["days_to_MFE"], 5)
        self.assertAlmostEqual(result["5D"]["final_return"], .05)
        self.assertEqual(result["instrument"], "UNDERLYING")

    def test_bottom_confirmation_requires_two_closes_and_ma20(self):
        rows = [row((dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat(),
                    100 if i < 18 else (90 if i == 18 else 110)) for i in range(20)]
        self.assertFalse(indicators(rows)["bottom_confirmation"])
        rows.append(row("2026-01-21", 112))
        self.assertTrue(indicators(rows)["bottom_confirmation"])

    @patch("foundation.closed_dates_since", return_value=["2026-01-02"])
    @patch("foundation.fetch_security")
    def test_daily_second_run_writes_zero(self, fetch, dates):
        drive = MemoryDrive()
        security = {"market": "US", "security_id": "US-000001", "ticker": "AAPL"}
        fetch.return_value = ([row("2026-01-02", 101)], ["PASS_DAILY"], [], None)
        last, keys = {security["security_id"]: "2026-01-01"}, set()
        first = append_daily_date(drive, "US", "2026-01-02", [security], last, keys,
                                  1, ["2026-01-01"])
        before = len(drive.writes)
        second = append_daily_date(drive, "US", "2026-01-02", [security], last, keys,
                                   1, ["2026-01-01"])
        self.assertEqual(first["written"], 1)
        self.assertEqual(second["written"], 0)
        self.assertEqual(len([x for x in drive.writes[before:] if "/DAILY/" in x]), 0)
        self.assertEqual(len(parse_lines_gz(drive.data["US/DAILY/2026-01-02/part-0001.ndjson.gz"])), 1)

    @patch("foundation.closed_dates_since", return_value=["2026-01-02"])
    @patch("foundation.fetch_security")
    def test_market_wide_outage_has_no_individual_repairs(self, fetch, dates):
        drive = MemoryDrive()
        securities = [{"market": "US", "security_id": f"US-{i:06d}", "ticker": f"X{i}"}
                      for i in range(10)]
        fetch.return_value = ([], ["FETCH_FAILED"], [], "YAHOO_404")
        result = append_daily_date(drive, "US", "2026-01-02", securities,
                                   {s["security_id"]: "2026-01-01" for s in securities},
                                   set(), 2, ["2026-01-01"])
        self.assertEqual(result["status"], "MARKET_WIDE_DATA_UNAVAILABLE")
        self.assertEqual(drive.json("REPAIR_QUEUE.json")["items"], [])


if __name__ == "__main__":
    unittest.main()
