"""Regressions for response loss and incomplete DAILY process status."""
import os
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hunter-global"))
import runner


class TargetedSyncTests(unittest.TestCase):
    def drive(self, failure, stored):
        drive = runner.Drive.__new__(runner.Drive)
        drive._call = Mock(side_effect=failure)
        drive.file = Mock(return_value={"id": "existing"} if stored is not None else None)
        drive.read = Mock(return_value=stored)
        return drive

    def test_append_response_lost_after_commit_is_confirmed_by_content(self):
        for error in (runner.requests.ReadTimeout("lost response"),
                      RuntimeError("BRIDGE_APPEND_CONFLICT")):
            with self.subTest(error=type(error).__name__):
                drive = self.drive(error, b"verified bytes")
                self.assertEqual(drive.append("US/DAILY/day/part", b"verified bytes"),
                                 {"id": "existing"})
                drive._call.assert_called_once()

    def test_append_never_accepts_different_content_or_missing_file(self):
        for stored in (b"other writer's bytes", None):
            with self.subTest(stored=stored):
                drive = self.drive(RuntimeError("BRIDGE_APPEND_CONFLICT"), stored)
                with self.assertRaisesRegex(RuntimeError, "APPEND_CONFLICT"):
                    drive.append("US/DAILY/day/part", b"expected")

    def test_failed_readback_does_not_report_success(self):
        drive = self.drive(runner.requests.ReadTimeout("response lost"), b"expected")
        drive.read.side_effect = runner.requests.ReadTimeout("readback unavailable")
        with self.assertRaises(runner.requests.ReadTimeout):
            drive.append("US/DAILY/day/part", b"expected")

    def test_policy_rejection_is_not_reconciled(self):
        drive = self.drive(RuntimeError("BRIDGE_SCOPE_PATH_DENIED"), b"expected")
        with self.assertRaisesRegex(RuntimeError, "SCOPE_PATH_DENIED"):
            drive.append("US/DAILY/day/part", b"expected")
        drive.file.assert_not_called()

    def test_source_outage_cannot_exit_successfully(self):
        env = {"APPS_SCRIPT_WEBAPP_URL": "test", "APPS_SCRIPT_SHARED_KEY": "test",
               "HUNTER_ACTIONS_CUTOVER": "CONFIRMED"}
        with patch.dict(os.environ, env), \
                patch.object(sys, "argv", ["runner", "--mode", "auto", "--market", "US"]), \
                patch("runner._enforce_cloud_run_topology"), \
                patch("production_guard.check_at_start"), patch("runner.Drive"), \
                patch("foundation.seed_corporate_actions"), \
                patch("foundation.run_daily", return_value=[{
                    "status": "MARKET_WIDE_DATA_UNAVAILABLE", "written": 0}]):
            with self.assertRaisesRegex(RuntimeError, "DAILY_INCOMPLETE:US"):
                runner.main()


if __name__ == "__main__":
    unittest.main()
