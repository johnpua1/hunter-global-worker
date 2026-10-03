import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from runner import TZ, compact, lines_gz, now_myt
from repair import repair_due, run_repair
from maintenance import repair_markets
from test_foundation import MemoryDrive


class MaintenanceResumeTests(unittest.TestCase):
    def drive(self, status="OPEN", **extra):
        drive = MemoryDrive()
        drive.data["US/CALENDAR_BASE.ndjson.gz"] = lines_gz([{"date": "2026-10-02"}])
        drive.data["REPAIR_QUEUE.json"] = compact({"items": [{
            "market": "US", "security_id": "US-000001", "category": "FETCH_FAILED",
            "trade_date": "2026-10-02", "status": status, **extra}]})
        return drive

    def run_once(self, drive, result="NO_DATA", accepted=False):
        with patch("repair.current_universe", return_value=[{"security_id": "US-000001"}]), \
             patch("repair.decide", return_value={
                 "result": result, "accepted": accepted,
                 "reason": "NO_PRIMARY_BAR_ACTIVE_LISTING", "verified_at_myt": now_myt()
             }):
            return run_repair(drive, "US")

    def test_backoff_survives_restart_and_stops_after_three_attempts(self):
        drive = self.drive()
        for attempt in range(1, 4):
            result = self.run_once(drive)
            item = drive.json("REPAIR_QUEUE.json")["items"][0]
            self.assertEqual(result["processed"], 1)
            self.assertEqual(item["repair_attempts"], attempt)
            self.assertEqual(item["status"], "NO_DATA")
            self.assertEqual(self.run_once(drive)["processed"], 0)
            if attempt < 3:
                item["next_retry_at_myt"] = "2000-01-01T00:00:00+08:00"
                drive.data["REPAIR_QUEUE.json"] = compact({"items": [item]})
        self.assertEqual(result["retry_exhausted"], 1)
        self.assertNotIn("next_retry_at_myt", item)
        self.assertTrue(all(p == "REPAIR_QUEUE.json" for p in drive.writes))

    def test_legacy_outage_is_retried_then_resolved_with_sidecar(self):
        drive = self.drive("UNRESOLVED", reason="MARKET_WIDE_OUTAGE",
                           verified_at_myt="2000-01-01T00:00:00+08:00")
        self.assertEqual(self.run_once(drive, "RESOLVED", True)["accepted"], 1)
        item = drive.json("REPAIR_QUEUE.json")["items"][0]
        self.assertEqual(item["repair_attempts"], 2)
        self.assertIn("/REPAIR_PATCH/", item["patch_path"])
        self.assertEqual(self.run_once(drive)["processed"], 0)

    def test_evidence_gates_and_terminal_rows_are_never_reopened(self):
        now = dt.datetime.now(TZ)
        for status, reason in [("UNRESOLVED", "OFFICIAL_IDENTITY_PROOF_REQUIRED"),
                               ("RESOLVED", "MARKET_WIDE_OUTAGE"),
                               ("VALIDATED_PRIMARY_ONLY", "MARKET_WIDE_OUTAGE"),
                               ("QUARANTINED", "MARKET_WIDE_OUTAGE")]:
            self.assertFalse(repair_due({"status": status, "reason": reason,
                             "verified_at_myt": "2000-01-01T00:00:00+08:00"}, now))
        self.assertFalse(repair_due({"status": "NO_DATA", "reason": "NO_PRIMARY_BAR_ACTIVE_LISTING",
                                    "verified_at_myt": "bad timestamp"}, now))

    def test_cas_conflict_preserves_concurrent_resolution(self):
        drive = self.drive("UNRESOLVED", reason="MARKET_WIDE_OUTAGE",
                           verified_at_myt="2000-01-01T00:00:00+08:00")
        original_put = drive.put_fast
        calls = []
        def conflict(path, data, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                doc = drive.json(path)
                doc["items"][0]["status"] = "RESOLVED"
                drive.data[path] = compact(doc)
                raise RuntimeError("BRIDGE_STALE_WRITE")
            original_put(path, data, **kwargs)
        drive.put_fast = conflict
        self.run_once(drive)
        self.assertEqual(drive.json("REPAIR_QUEUE.json")["items"][0]["status"], "RESOLVED")

    def test_first_market_error_still_runs_second_with_remaining_budget(self):
        with patch("maintenance.time.monotonic", side_effect=[0, 900]), \
             patch("maintenance.run_repair", side_effect=[RuntimeError("BRIDGE_TIMEOUT"), {}]) as run:
            with self.assertRaisesRegex(RuntimeError, "MAINTENANCE_REPAIR_INCOMPLETE:US"):
                repair_markets(None, ["US", "HK"], 2400)
        self.assertEqual([c.args[1] for c in run.call_args_list], ["US", "HK"])
        self.assertEqual([c.kwargs["deadline"] for c in run.call_args_list], [1200, 2400])

    def test_budget_exhaustion_is_a_failure_so_cloud_run_resumes(self):
        with patch("maintenance.time.monotonic", return_value=0), \
             patch("maintenance.run_repair", return_value={"timed_out": True}) as run:
            with self.assertRaisesRegex(RuntimeError, "MAINTENANCE_REPAIR_INCOMPLETE"):
                repair_markets(None, ["US", "HK"], 2400)
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
