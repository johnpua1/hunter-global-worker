import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hunter-global"))
import daily_data_only as d


class MemoryDrive:
    def __init__(self, market="HK"):
        self.market, self.target, self.files, self.calls = market, None, {}, []
        self.fail_append_ack = False
        self.crash_after_first_progress = False

    def gate(self, op, path):
        if not d.DailyDrive.allowed(self, op, path):
            raise AssertionError("Forbidden access: " + op + " " + path)
        self.calls.append((op, path))

    def file(self, path):
        self.gate("file", path)
        raw = self.files.get(path)
        return None if raw is None else {"md5": hashlib.md5(raw).hexdigest(), "size": len(raw)}

    def read(self, path):
        self.gate("read", path)
        return self.files[path]

    def json(self, path):
        return json.loads(self.read(path))

    def list(self, path):
        self.gate("list", path)
        return [{"name": p[len(path)+1:]} for p in self.files if p.startswith(path + "/")]

    def put(self, path, data, immutable=False, expected_sha=None, **kwargs):
        self.gate("put", path)
        if immutable and path in self.files and self.files[path] != data:
            raise RuntimeError("IMMUTABLE_CONFLICT")
        if expected_sha is not None and d.digest(self.files[path]) != expected_sha:
            raise RuntimeError("STALE_WRITE")
        self.files[path] = data
        if self.crash_after_first_progress and path.endswith("STATE.json") and json.loads(data)["next_part"] == 2:
            self.crash_after_first_progress = False
            raise RuntimeError("SIMULATED_CRASH")

    def append(self, path, data, mime):
        self.gate("append", path)
        if path in self.files:
            raise AssertionError("Duplicate append")
        self.files[path] = data
        if self.fail_append_ack:
            self.fail_append_ack = False
            raise RuntimeError("SIMULATED_LOST_ACK")


def setup(n=3, market="HK"):
    drive = MemoryDrive(market)
    securities = [{"market": market, "security_id": str(i), "ticker": str(i)} for i in range(n)]
    drive.files[f"{market}/CURRENT_UNIVERSE.json"] = d.compact({"market": market, "securities": securities})
    drive.files[f"{market}/CONTROL/DAILY_CHECKPOINT.json"] = d.compact({"market": market, "last_completed_date": "2026-10-07", "phase2_completed_date": "2026-10-06"})
    return drive


def bar(s, date):
    return {"security_id": s["security_id"], "date": date, "trade_date": date,
            "open": 10, "high": 11, "low": 9, "close": 10, "volume": 10}


class DailyTests(unittest.TestCase):
    def test_completed_receipt_skips_universe_source_and_outputs(self):
        drive = setup()
        drive.files["HK/CONTROL/DAILY_RUN_2026-10-08.json"] = d.compact({"market": "HK", "trade_date": "2026-10-08", "status": "COMPLETE"})
        original = copy.deepcopy(drive.files)
        out = d.run(drive, "HK", find_sessions=lambda *_: ["2026-10-08"], fetch=lambda *_: self.fail("fetch"))
        self.assertEqual(out["last_completed_date"], "2026-10-08")
        self.assertFalse(any("CURRENT_UNIVERSE" in p or "/DAILY/" in p for _, p in drive.calls))
        self.assertEqual(drive.files["HK/CONTROL/DAILY_CHECKPOINT.json"], original["HK/CONTROL/DAILY_CHECKPOINT.json"])

    def test_next_day_no_archive_reads_both_markets(self):
        for market in ("US", "HK"):
            drive = setup(market=market)
            d.run(drive, market, find_sessions=lambda *_: ["2026-10-08"], fetch=bar)
            self.assertEqual(len([1 for op, _ in drive.calls if op == "append"]), 1)
            self.assertFalse(any(op == "read" and "/DAILY/" in p for op, p in drive.calls))
            self.assertFalse(any("PHASE2" in p or "BASE/" in p or "REPAIR_PATCH" in p for _, p in drive.calls))

    def test_lost_append_ack_uses_stage_not_refetch_or_readback(self):
        drive = setup()
        drive.fail_append_ack = True
        with self.assertRaisesRegex(RuntimeError, "LOST_ACK"):
            d.process_date(drive, "HK", "2026-10-08", fetch=bar)
        result = d.process_date(drive, "HK", "2026-10-08", fetch=lambda *_: self.fail("refetch"))
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(len([1 for op, _ in drive.calls if op == "append"]), 1)

    def test_committed_batch_skipped_after_restart(self):
        drive = setup(102)
        calls = []
        def fetch(s, date):
            calls.append(s["security_id"])
            return bar(s, date)
        drive.crash_after_first_progress = True
        with self.assertRaisesRegex(RuntimeError, "SIMULATED_CRASH"):
            d.process_date(drive, "HK", "2026-10-08", fetch=fetch)
        d.process_date(drive, "HK", "2026-10-08", fetch=fetch)
        self.assertEqual(len(calls), 102)
        self.assertEqual(len(set(calls)), 102)

    def test_existing_partial_day_blocks_without_read(self):
        drive = setup()
        drive.files["HK/DAILY/2026-10-08/part-0001.ndjson.gz"] = b"existing"
        with self.assertRaisesRegex(RuntimeError, "LEGACY_PARTIAL"):
            d.process_date(drive, "HK", "2026-10-08", fetch=lambda *_: self.fail("fetch"))
        self.assertFalse(any(op in ("read", "append") and "/DAILY/" in p for op, p in drive.calls))

    def test_outage_does_not_advance_and_retries_only_missing(self):
        drive = setup(10)
        def outage(s, day):
            return bar(s, day) if s["security_id"] == "0" else None
        with self.assertRaisesRegex(RuntimeError, "SOURCE_UNAVAILABLE"):
            d.run(drive, "HK", find_sessions=lambda *_: ["2026-10-08"], fetch=outage)
        self.assertEqual(json.loads(drive.files["HK/CONTROL/DAILY_DATA_CHECKPOINT.json"])["last_completed_date"], "2026-10-07")
        seen = []
        def restored(s, day):
            seen.append(s["security_id"])
            return bar(s, day)
        d.run(drive, "HK", find_sessions=lambda *_: ["2026-10-08"], fetch=restored)
        self.assertNotIn("0", seen)
        self.assertEqual(len(seen), 9)

    def test_gaps_explicit_not_full_coverage(self):
        drive = setup(3)
        result = d.process_date(drive, "HK", "2026-10-08", fetch=lambda s, day: None if s["security_id"] == "0" else bar(s, day))
        self.assertEqual(result["status"], "COMPLETE_WITH_GAPS")
        self.assertEqual(result["missing_security_ids"], ["0"])

    def test_scope_denies_history_phase2_and_repair(self):
        drive = setup()
        drive.target = "2026-10-08"
        for path in ("HK/DAILY/2026-10-07/part-0001.ndjson.gz", "HK/BASE/batch-0001.ndjson.gz",
                     "HK/PHASE2/EARNINGS_HISTORY.json", "REPAIR_QUEUE.json", "HK/DERIVED/2026-10-08/RANK.json"):
            for op in ("read", "put", "list", "append"):
                self.assertFalse(d.DailyDrive.allowed(drive, op, path))

    def test_sessions_cutoff_and_no_padding(self):
        args = []
        def source(symbol, market, first, last):
            args.append((first, last))
            return {"timestamp": [int(dt.datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("Asia/Hong_Kong")).timestamp())]}
        after = "2026-10-07"
        self.assertEqual(d.sessions("HK", after, dt.datetime(2026, 10, 8, 16, tzinfo=ZoneInfo("Asia/Hong_Kong")), source), [])
        self.assertEqual(args, [])
        self.assertEqual(d.sessions("HK", after, dt.datetime(2026, 10, 8, 18, tzinfo=ZoneInfo("Asia/Hong_Kong")), source), ["2026-10-08"])
        self.assertEqual(args, [("2026-10-08", "2026-10-08")])

    def test_fetch_requests_only_day_and_filters_provider_extras(self):
        zone = ZoneInfo("Asia/Hong_Kong")
        def source(symbol, market, first, last):
            self.assertEqual((first, last), ("2026-10-08", "2026-10-08"))
            return {"timestamp": [int(dt.datetime(2026, 10, day, 9, 30, tzinfo=zone).timestamp()) for day in (7, 8)],
                    "indicators": {"quote": [{k: [10, 10] for k in ("open", "high", "low", "close", "volume")}]}}
        result = d.fetch_bar({"market": "HK", "ticker": "0001.HK", "security_id": "HK-1"}, "2026-10-08", source)
        self.assertEqual(result["date"], "2026-10-08")

    def test_isolated_image_imports_no_retired_modules(self):
        with tempfile.TemporaryDirectory() as root:
            for filename in ("daily_transport.py", "daily_data_only.py", "continuation.py", "execution_budget.py", "write_recovery.py"):
                shutil.copy(ROOT / "hunter-global" / filename, pathlib.Path(root) / filename)
            shutil.copy(ROOT / "hunter-global/daily_transport.py", pathlib.Path(root) / "runner.py")
            code = "import daily_data_only, runner, continuation, write_recovery, sys; assert not hasattr(runner, 'main'); assert not ({'foundation','derived','phase2_runtime','incremental_inputs'} & set(sys.modules))"
            p = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, env=os.environ.copy())
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_lease_cas_and_owner_release(self):
        drive = setup()
        path = "HK/CONTROL/DAILY_DATA_LEASE.json"
        first, second = d.Record(drive, path), d.Record(drive, path)
        first.save({"owner": "one", "expires": "2099-01-01T00:00:00+00:00"})
        with self.assertRaisesRegex(RuntimeError, "IMMUTABLE_CONFLICT"):
            second.save({"owner": "two", "expires": "2099-01-01T00:00:00+00:00"})
        d.release_lease(drive, "HK", "two")
        self.assertEqual(json.loads(drive.files[path])["expires"], "2099-01-01T00:00:00+00:00")
        d.release_lease(drive, "HK", "one")
        self.assertNotEqual(json.loads(drive.files[path])["expires"], "2099-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
