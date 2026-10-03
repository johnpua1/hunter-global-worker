import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from runner import compact, lines_gz
from repair import append_repair_patch, run_repair
from test_foundation import MemoryDrive


class LostResponseDrive(MemoryDrive):
    def append(self, path, content, mime=None):
        super().append(path, content, mime)
        # The server committed, the first response was lost, and the retry
        # received the immutable Bridge's existing-file rejection.
        raise RuntimeError("BRIDGE_APPEND_CONFLICT")


class RepairAppendRecoveryTests(unittest.TestCase):
    def test_committed_write_with_lost_response_is_read_back_not_rewritten(self):
        drive = LostResponseDrive()
        path = "US/REPAIR_PATCH/aa/example.json"
        append_repair_patch(drive, path, b'{"accepted":true}')
        self.assertEqual(drive.data[path], b'{"accepted":true}')
        self.assertEqual(drive.writes, [path])

    def test_different_content_is_not_accepted_or_overwritten(self):
        drive = MemoryDrive()
        path = "US/REPAIR_PATCH/aa/example.json"
        drive.data[path] = b'{"accepted":false}'
        with patch.object(drive, "append", side_effect=RuntimeError("BRIDGE_APPEND_CONFLICT")):
            with self.assertRaisesRegex(RuntimeError, "PATCH_APPEND_CONTENT_CONFLICT"):
                append_repair_patch(drive, path, b'{"accepted":true}')
        self.assertEqual(drive.data[path], b'{"accepted":false}')
        self.assertEqual(drive.writes, [])

    def test_unrelated_error_is_not_hidden_by_an_existing_file(self):
        drive = MemoryDrive()
        with patch.object(drive, "append", side_effect=RuntimeError("BRIDGE_ACCESS_DENIED")), \
             patch.object(drive, "read") as read:
            with self.assertRaisesRegex(RuntimeError, "BRIDGE_ACCESS_DENIED"):
                append_repair_patch(drive, "US/REPAIR_PATCH/aa/example.json", b"same")
            read.assert_not_called()

    def test_missing_readback_does_not_become_success(self):
        drive = MemoryDrive()
        with patch.object(drive, "append", side_effect=RuntimeError("BRIDGE_APPEND_CONFLICT")):
            with self.assertRaises(KeyError):
                append_repair_patch(drive, "US/REPAIR_PATCH/aa/missing.json", b"same")

    def test_repair_queue_resumes_after_verified_append_without_touching_base(self):
        drive = LostResponseDrive()
        drive.data["US/CALENDAR_BASE.ndjson.gz"] = lines_gz([{"date": "2026-10-02"}])
        drive.data["REPAIR_QUEUE.json"] = compact({"items": [{
            "market": "US", "security_id": "US-000001", "category": "FETCH_FAILED",
            "trade_date": "2026-10-02", "status": "OPEN"}]})
        answer = {"market": "US", "security_id": "US-000001", "category": "FETCH_FAILED",
                  "result": "RESOLVED", "accepted": True, "rows": [],
                  "verified_at_myt": "2026-10-03T17:00:00+08:00"}
        with patch("repair.current_universe", return_value=[{"security_id": "US-000001"}]), \
             patch("repair.decide", return_value=answer):
            result = run_repair(drive, "US")
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["accepted"], 1)
        item = drive.json("REPAIR_QUEUE.json")["items"][0]
        self.assertEqual(item["status"], "RESOLVED")
        self.assertEqual(drive.json(item["patch_path"]), answer)
        self.assertTrue(all(p == "REPAIR_QUEUE.json" or "/REPAIR_PATCH/" in p for p in drive.writes))


if __name__ == "__main__":
    unittest.main()
