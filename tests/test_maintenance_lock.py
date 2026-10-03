import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
import maintenance_lock as lock
from runner import compact, digest

A, B = "hunter-maintenance-first", "hunter-maintenance-second"
TA, TB = "a" * 32, "b" * 32


class CASDrive:
    def __init__(self, concurrent=False):
        self.raw = None
        self.mutex = threading.Lock()
        self.barrier = threading.Barrier(2) if concurrent else None
        self.initial_reads = 0
        self.lose_reply = False

    def read(self, path):
        assert path == lock.LOCK_PATH
        with self.mutex:
            raw = self.raw
            self.initial_reads += 1
            wait = self.barrier is not None and self.initial_reads <= 2
        if wait:
            self.barrier.wait(timeout=3)
        if raw is None:
            raise RuntimeError("BRIDGE_FILE_NOT_FOUND")
        return raw

    def _call(self, op, **fields):
        assert op == "put" and fields["path"] == lock.LOCK_PATH
        assert "expected_sha256" in fields
        raw = base64.b64decode(fields["data_base64"])
        assert digest(raw) == fields["sha256"]
        with self.mutex:
            previous = digest(self.raw) if self.raw is not None else None
            if fields["expected_sha256"] != previous:
                raise RuntimeError("BRIDGE_STALE_WRITE")
            self.raw = raw
            if self.lose_reply:
                self.lose_reply = False
                raise RuntimeError("BRIDGE_STALE_WRITE")
        return {"sha256": fields["sha256"]}


class MaintenanceLockTests(unittest.TestCase):
    def test_simultaneous_scheduler_and_manual_have_one_winner(self):
        drive = CASDrive(concurrent=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: lock.acquire(drive, pair[0], 0, pair[1], lambda _: False),
                                    [(A, TA), (B, TB)]))
        self.assertEqual(sorted(results), [False, True])

    def test_running_owner_never_expires_and_terminal_owner_can_be_replaced(self):
        drive = CASDrive()
        self.assertTrue(lock.acquire(drive, A, 0, TA))
        self.assertFalse(lock.acquire(drive, B, 0, TB, lambda _: False))
        self.assertTrue(lock.acquire(drive, B, 0, TB, lambda _: True))
        self.assertEqual(lock.read_lock(drive)[1]["execution"], B)
        with self.assertRaisesRegex(RuntimeError, "OWNERSHIP_LOST"):
            lock.release(drive, A, 0, TA)
        self.assertEqual(lock.read_lock(drive)[1]["execution"], B)

    def test_crashed_attempt_resumes_only_with_higher_attempt(self):
        drive = CASDrive()
        lock.acquire(drive, A, 0, TA)
        self.assertFalse(lock.acquire(drive, A, 0, TB))
        self.assertTrue(lock.acquire(drive, A, 1, TB))
        self.assertFalse(lock.acquire(drive, A, 0, TA))

    def test_lost_acquire_and_release_responses_reconcile(self):
        drive = CASDrive()
        drive.lose_reply = True
        self.assertTrue(lock.acquire(drive, A, 0, TA))
        drive.lose_reply = True
        lock.release(drive, A, 0, TA)
        self.assertIsNone(lock.read_lock(drive)[1])

    def test_owner_lookup_error_does_not_steal_lock(self):
        drive = CASDrive()
        lock.acquire(drive, A, 0, TA)
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE"):
            lock.acquire(drive, B, 0, TB, lambda _: (_ for _ in ()).throw(ValueError("UNAVAILABLE")))
        self.assertEqual(lock.read_lock(drive)[1]["execution"], A)

    def test_business_exception_releases_lock_and_preserves_error(self):
        drive = CASDrive()
        with patch.dict("os.environ", {"CLOUD_RUN_EXECUTION": A, "CLOUD_RUN_TASK_ATTEMPT": "0"}):
            with self.assertRaisesRegex(RuntimeError, "BUSINESS_FAILED"):
                with lock.maintenance_slot(drive) as held:
                    self.assertTrue(held)
                    raise RuntimeError("BUSINESS_FAILED")
        self.assertIsNone(lock.read_lock(drive)[1])

    def test_invalid_lock_is_not_overwritten(self):
        drive = CASDrive()
        drive.raw = compact({"schema": 1, "owner": {"execution": "other-job"}})
        with self.assertRaisesRegex(RuntimeError, "OWNER_INVALID"):
            lock.acquire(drive, A, 0, TA)

    def test_execution_read_requires_exact_identity_and_completion_time(self):
        name = "projects/rgs-hunter-global/locations/us-central1/jobs/hunter-maintenance/executions/" + A
        for doc, expected in [({"name": name, "runningCount": 0, "retriedCount": 1}, False),
                              ({"name": name, "completionTime": "2026-10-03T12:00:00Z"}, True)]:
            with patch.object(lock, "_get_json", side_effect=[{"access_token": "test"}, doc]):
                self.assertEqual(lock.execution_finished(A), expected)
        with patch.object(lock, "_get_json", side_effect=[{"access_token": "test"}, {"name": "other"}]):
            with self.assertRaisesRegex(RuntimeError, "EXECUTION_MISMATCH"):
                lock.execution_finished(A)

    def test_live_probe_touches_only_lock_and_leaves_it_released(self):
        drive = CASDrive()
        with patch.dict("os.environ", {"CLOUD_RUN_EXECUTION": A, "CLOUD_RUN_TASK_ATTEMPT": "0"}), \
             patch.object(lock, "execution_finished", return_value=False):
            lock.verify_lock(drive)
        self.assertIsNone(lock.read_lock(drive)[1])

    def test_busy_execution_never_enters_business_work(self):
        import maintenance
        drive = CASDrive()
        drive.health = lambda: None
        lock.acquire(drive, A, 0, TA)
        with patch.dict("os.environ", {"CLOUD_RUN_JOB": "hunter-maintenance",
             "CLOUD_RUN_EXECUTION": A, "CLOUD_RUN_TASK_ATTEMPT": "0",
             "HUNTER_ACTIONS_CUTOVER": "CONFIRMED", "HUNTER_MAINTENANCE_LOCK_PROBE": ""}), \
             patch("production_guard.check_at_start"), patch.object(maintenance, "Drive", return_value=drive), \
             patch.object(maintenance, "run_maintenance") as business:
            maintenance.main()
        business.assert_not_called()
        self.assertEqual(lock.read_lock(drive)[1]["token"], TA)


if __name__ == "__main__":
    unittest.main()
