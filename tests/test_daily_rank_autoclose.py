"""Current-date ranking recovery must be exact, idempotent and history-free."""
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hunter-global"))
from daily_rank_autoclose import NotReady, close_one, load_dated_indicators, outputs, rank_current
from runner import compact, digest


class FakeDrive:
    def __init__(self, date="2026-10-12"):
        self.date = date
        prefix = f"HK/CONTROL/PHASE2_RESUME/DERIVED_BATCHES/{date}"
        self.root = prefix
        self.files = {
            "HK/CONTROL/DAILY_CHECKPOINT.json": compact(
                {"market": "HK", "last_completed_date": "2026-10-09", "updated_at_myt": "earlier"}),
            f"HK/CONTROL/DAILY_RUN_{date}.json": compact(
                {"market": "HK", "trade_date": date, "status": "COMPLETE",
                 "active": 2, "available": 2}),
        }
        self.rows = [
            {"security_id": "HK-000001", "trade_date": date,
             "ma": {"20": 1}, "dollar_volume_20d": 1_000_000,
             "return_20d": .1, "alignment": "BULL",
             "bottom_confirmation": True, "mae_mfe": []},
            {"security_id": "HK-000002", "trade_date": "2026-10-09",
             "ma": {"20": 1}, "dollar_volume_20d": 1_000,
             "return_20d": -.1, "alignment": "BEAR",
             "bottom_confirmation": False, "mae_mfe": []},
        ]
        doc = {"schema": 1, "input_sha256": "a"*64,
               "rows_sha256": digest(compact(self.rows)), "rows": self.rows}
        self.shard = prefix + "/batch-0001/" + "b"*64 + ".json"
        self.files[self.shard] = compact(doc)
        self.accessed = []
        self.written = []

    def list(self, path):
        self.accessed.append(path)
        if path == self.root:
            return [{"name": "batch-0001",
                     "mimeType": "application/vnd.google-apps.folder"}]
        if path == self.root+"/batch-0001":
            return [{"name": "b"*64+".json", "mimeType": "application/json"}]
        raise AssertionError("UNAUTHORIZED_FOLDER:" + path)

    def file(self, path):
        self.accessed.append(path)
        return {"id": path} if path in self.files else None

    def read(self, path):
        self.accessed.append(path)
        if path not in self.files:
            raise AssertionError("UNAUTHORIZED_READ:" + path)
        return self.files[path]

    def json(self, path):
        return json.loads(self.read(path))

    def append(self, path, data):
        assert path not in self.files
        self.files[path] = data
        self.written.append(path)

    def put(self, path, data, expected_sha=None):
        assert path in self.files and expected_sha == digest(self.files[path])
        self.files[path] = data
        self.written.append(path)


class TestAutoRanking(unittest.TestCase):
    def test_exact_score_and_sort(self):
        fake = FakeDrive()
        rows = load_dated_indicators(fake, "HK", fake.date,
                                     {"active": 2, "available": 2})
        ranked = rank_current(rows, fake.date)
        self.assertEqual([r["security_id"] for r in ranked],
                         ["HK-000001", "HK-000002"])
        self.assertEqual(ranked[0]["rank_score"], 5.0)
        self.assertIsNone(ranked[1]["rank_score"])
        self.assertFalse(ranked[1]["filter_pass"])
        self.assertEqual(json.loads(outputs("HK", fake.date, ranked)[-1][1])["detail_parts"], 1)

    def test_current_date_only_and_idempotent(self):
        fake = FakeDrive()
        with patch("daily_rank_autoclose.closed_dates_since",
                   side_effect=[["2026-10-12"], []]):
            self.assertIn("CLOSED market=HK date=2026-10-12",
                          close_one(fake, "HK"))
            self.assertEqual("UP_TO_DATE", close_one(fake, "HK"))
        self.assertIn("HK/DERIVED/2026-10-12/RANK.json", fake.files)
        self.assertEqual(fake.json("HK/CONTROL/DAILY_CHECKPOINT.json")
                         ["last_completed_date"], "2026-10-12")
        self.assertFalse(any("/BASE/" in p or "/DAILY/" in p or
                             "/REPAIR_PATCH/" in p for p in fake.accessed))
        self.assertFalse(any(p.startswith("HK/PHASE2/") for p in fake.accessed))

    def test_no_partial_promotion(self):
        fake = FakeDrive()
        fake.list = lambda p: []
        with self.assertRaises(NotReady):
            load_dated_indicators(fake, "HK", fake.date,
                                  {"active": 2, "available": 2})
        self.assertEqual(fake.written, [])


if __name__ == "__main__":
    unittest.main()
