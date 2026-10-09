import importlib.util
import json
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HG = ROOT / "hunter-global"
sys.path.insert(0, str(HG))

spec = importlib.util.spec_from_file_location("daily_closeout", HG / "daily_closeout.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeDrive:
    def __init__(self):
        self.docs = {
            "US/CONTROL/DAILY_CHECKPOINT.json":
                {"market": "US", "last_completed_date": "2026-10-08",
                 "updated_at_myt": "2026-10-09T09:59:47+08:00"},
            "US/CONTROL/DAILY_RUN_2026-10-09.json":
                {"market": "US", "trade_date": "2026-10-09", "status": "COMPLETE",
                 "active": 5358, "available": 5257, "written": 5340, "repairs": 287},
            "US/DERIVED/2026-10-09/RANK.json":
                {"market": "US", "as_of": "2026-10-09",
                 "rows": [{"security_id": f"US-{i:06d}"} for i in range(5257)],
                 "detail_parts": 22},
        }
        for i in range(1, 23):
            self.docs[f"US/DERIVED/2026-10-09/batch-{i:04d}.json"] = {"ok": True}
        self.puts = []

    @staticmethod
    def _raw(v):
        return json.dumps(v, separators=(",", ":")).encode()

    def file(self, path):
        return {"name": pathlib.PurePosixPath(path).name} if path in self.docs else None

    def read(self, path):
        return self._raw(self.docs[path])

    def json(self, path):
        return self.docs[path]

    def put(self, path, content, expected_sha=None):
        self.docs[path] = json.loads(content)
        self.puts.append(path)
        return {"path": path}


class DailyCloseoutTests(unittest.TestCase):
    def test_complete_receipt_closes_without_refetch(self):
        drive = FakeDrive()
        original = sys.modules.get("runner")
        runner = types.ModuleType("runner")
        runner.compact = lambda v: json.dumps(v, separators=(",", ":")).encode()
        import hashlib
        runner.digest = lambda b: hashlib.sha256(b).hexdigest()
        runner.now_myt = lambda: "2026-10-10T08:00:00+08:00"
        calls = []
        runner.closed_dates_since = lambda market, last: (calls.append((market, last)) or
                                                          (["2026-10-09"] if last == "2026-10-08" else []))
        sys.modules["runner"] = runner
        try:
            closed = mod.closeout_committed_receipts(drive, "US")
        finally:
            if original is None:
                sys.modules.pop("runner", None)
            else:
                sys.modules["runner"] = original
        self.assertEqual(closed, ["2026-10-09"])
        self.assertEqual(drive.docs["US/CONTROL/DAILY_CHECKPOINT.json"]["last_completed_date"],
                         "2026-10-09")
        self.assertEqual(drive.puts, ["US/CONTROL/DAILY_CHECKPOINT.json"])
        self.assertEqual(calls, [("US", "2026-10-08"), ("US", "2026-10-09")])

    def test_missing_receipt_is_noop(self):
        drive = FakeDrive()
        drive.docs.pop("US/CONTROL/DAILY_RUN_2026-10-09.json")
        original = sys.modules.get("runner")
        runner = types.ModuleType("runner")
        runner.compact = lambda v: json.dumps(v, separators=(",", ":")).encode()
        import hashlib
        runner.digest = lambda b: hashlib.sha256(b).hexdigest()
        runner.now_myt = lambda: "2026-10-10T08:00:00+08:00"
        runner.closed_dates_since = lambda market, last: ["2026-10-09"]
        sys.modules["runner"] = runner
        try:
            closed = mod.closeout_committed_receipts(drive, "US")
        finally:
            if original is None:
                sys.modules.pop("runner", None)
            else:
                sys.modules["runner"] = original
        self.assertEqual(closed, [])
        self.assertEqual(drive.puts, [])


if __name__ == "__main__":
    unittest.main()
