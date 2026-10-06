import gzip
import importlib
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hunter-global"))
foundation = importlib.import_module("foundation")


class FakeDrive:
    def __init__(self):
        self.appended = []
        self.puts = []

    def list(self, folder):
        return []

    def append(self, path, payload, mime="application/json"):
        self.appended.append((path, payload, mime))
        return {"path": path}

    def put(self, path, payload, *args, **kwargs):
        self.puts.append((path, payload))
        return {"path": path}

    def file(self, path):
        return None


def decode_gzip_lines(payload):
    raw = gzip.decompress(payload).decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line]


class DailyFolderIdentityTests(unittest.TestCase):
    def test_catchup_rows_are_partitioned_by_row_trade_date(self):
        drive = FakeDrive()
        securities = [{
            "security_id": "HK-TEST",
            "market": "HK",
            "ticker": "0001.HK",
            "listing_status": "ACTIVE",
        }]
        last = {"HK-TEST": "2026-10-02"}
        keys = set()

        def fake_fetch_security(security, calendar, as_of, daily=False):
            self.assertEqual(as_of, "2026-10-05")
            return ([
                {"security_id": "HK-TEST", "date": "2026-10-03",
                 "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
                 "volume": 100, "adjustment_as_of": as_of},
                {"security_id": "HK-TEST", "date": "2026-10-05",
                 "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
                 "volume": 120, "adjustment_as_of": as_of},
            ], ["PASS_DAILY"], [], None)

        with mock.patch.object(foundation, "fetch_security", side_effect=fake_fetch_security), \
             mock.patch.object(foundation, "closed_dates_since",
                               return_value=["2026-10-03", "2026-10-05"]), \
             mock.patch.object(foundation, "append_queue", return_value=0), \
             mock.patch.object(foundation, "flag_five_day_failures", return_value=0):
            result = foundation.append_daily_date(
                drive, "HK", "2026-10-05", securities, last, keys, 1, ["2026-10-02"]
            )

        paths = [path for path, _, _ in drive.appended]
        self.assertEqual(paths, [
            "HK/DAILY/2026-10-03/part-0001.ndjson.gz",
            "HK/DAILY/2026-10-05/part-0001.ndjson.gz",
        ])
        for path, payload, _ in drive.appended:
            folder_date = path.split("/")[2]
            rows = decode_gzip_lines(payload)
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row["date"], folder_date)
                self.assertEqual(row["trade_date"], folder_date)
                self.assertTrue(row["security_id"].startswith("HK-"))
        self.assertEqual(result["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
