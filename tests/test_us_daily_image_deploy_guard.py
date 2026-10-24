"""Read-only safety gate for updating the existing US DAILY Cloud Run Job."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "cloudrun" / "deploy-us-phase2-fix-20261015.py"
spec = importlib.util.spec_from_file_location("us_image_deploy_guard", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class USImageDeployGuardTests(unittest.TestCase):
    def test_terminal_evidence_accepts_failures_as_terminal_not_success(self):
        self.assertTrue(mod._terminal_execution({
            "completionStatus": "EXECUTION_FAILED"}))
        self.assertTrue(mod._terminal_execution({
            "status": {"conditions": [
                {"type": "Completed", "status": "False"}]}}))
        self.assertTrue(mod._terminal_execution({
            "completionTime": "2026-10-24T00:00:00Z"}))
        self.assertFalse(mod._terminal_execution({
            "completionStatus": "EXECUTION_RUNNING"}))

    def test_running_or_unverified_blocks_before_update(self):
        for status in ("EXECUTION_RUNNING", "EXECUTION_PENDING", "UNKNOWN"):
            with self.subTest(status=status):
                row = {"name": "hunter-us-daily-active", "completionStatus": status}
                calls = []
                def get(args, text=True):
                    calls.append(args)
                    return json.dumps([row] if "list" in args else row)
                with patch.object(mod.subprocess, "check_output", side_effect=get):
                    with self.assertRaisesRegex(
                            RuntimeError, "US_DEPLOY_EXECUTION_NOT_PROVEN_TERMINAL"):
                        mod.require_no_active_us_execution()
                self.assertEqual(len(calls), 2)

    def test_list_false_pending_can_be_resolved_by_execution_detail(self):
        pending = {"name": "hunter-us-daily-x123",
                   "completionStatus": "EXECUTION_PENDING"}
        done = {"metadata": {"name": "hunter-us-daily-x123"},
                "status": {"conditions": [
                    {"type": "Completed", "status": "False"}]}}
        def get(args, text=True):
            return json.dumps([pending] if "list" in args else done)
        with patch.object(mod.subprocess, "check_output", side_effect=get) as reads:
            mod.require_no_active_us_execution()
        self.assertEqual(reads.call_count, 2)

    def test_malformed_identity_fails_closed(self):
        row = {"name": "hunter-hk-daily-x1",
               "completionStatus": "EXECUTION_PENDING"}
        with patch.object(mod.subprocess, "check_output",
                          return_value=json.dumps([row])):
            with self.assertRaisesRegex(RuntimeError, "IDENTITY_UNVERIFIED"):
                mod.require_no_active_us_execution()

    def test_terminal_executions_do_not_block_deploy(self):
        rows = [{"name": "hunter-us-daily-old",
                 "completionStatus": "EXECUTION_FAILED"},
                {"name": "hunter-us-daily-new",
                 "completionStatus": "EXECUTION_SUCCEEDED"}]
        with patch.object(mod.subprocess, "check_output",
                          return_value=json.dumps(rows)) as reads:
            mod.require_no_active_us_execution()
        reads.assert_called_once()


if __name__ == "__main__":
    unittest.main()
