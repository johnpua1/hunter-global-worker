"""Fail-closed tests for synchronizing only the existing HK DAILY image.

No tests dispatch Cloud Run Jobs, create resources, or mutate market data.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "cloudrun" / "rebind-hk-existing-image.py"
spec = importlib.util.spec_from_file_location("hk_image_rebind", SOURCE)
rebind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebind)

SHA = "a" * 40
OLD = "b" * 40


def job(market: str, sha: str) -> dict:
    return {
        "spec": {"template": {"spec": {
            "taskCount": 1,
            "parallelism": 1,
            "template": {"spec": {
                "timeoutSeconds": 7200,
                "maxRetries": 0,
                "serviceAccountName": (
                    f"hunter-{market.lower()}-daily@rgs-hunter-global.iam.gserviceaccount.com"
                ),
                "containers": [{
                    "image": rebind.IMAGE_BASE + ":" + sha,
                    "args": ["--mode", "auto", "--market", market],
                    "env": [{"name": "HUNTER_SOURCE_SHA", "value": sha}],
                    "resources": {"limits": {"cpu": "1", "memory": "2Gi"}},
                }],
            }},
        }}},
    }


class HKImageRebindTests(unittest.TestCase):
    def test_active_or_indeterminate_execution_blocks_all_mutation(self):
        for row in (
            {"name": "hunter-hk-daily-active", "completionStatus": "EXECUTION_PENDING"},
            {"name": "hunter-hk-daily-unknown", "conditions": [{"type": "Completed", "state": "UNKNOWN"}]},
        ):
            with self.subTest(row=row):
                with patch.object(rebind, "describe", side_effect=[job("US", SHA), job("HK", OLD)]), \
                     patch.object(rebind, "gcloud", return_value=__import__("json").dumps([row])), \
                     patch.object(rebind.subprocess, "run") as update:
                    with self.assertRaisesRegex(RuntimeError, "NOT_PROVEN_TERMINAL"):
                        rebind.main(SHA)
                    update.assert_not_called()

    def test_image_and_sha_only_rebind_after_twice_confirming_terminal(self):
        before = job("HK", OLD)
        expected = job("HK", SHA)
        # Both the initial and pre-update execution checks must be terminal.
        finished = [{"name": "hk-old", "completionTime": "2026-10-20T00:00:00Z"}]
        with patch.object(rebind, "describe", side_effect=[
                job("US", SHA), before, copy.deepcopy(before), expected]), \
             patch.object(rebind, "gcloud", return_value=__import__("json").dumps(finished)) as execution_reads, \
             patch.object(rebind.subprocess, "run") as update:
            rebind.main(SHA)
            self.assertEqual(execution_reads.call_count, 2)
            update.assert_called_once()
            args = update.call_args.args[0]
            self.assertIn("hunter-hk-daily", args)
            self.assertIn("--image=" + rebind.IMAGE_BASE + ":" + SHA, args)
            self.assertIn("--update-env-vars=HUNTER_SOURCE_SHA=" + SHA, args)
            self.assertNotIn("execute", args)
            self.assertNotIn("--args", args)
            self.assertNotIn("--service-account", args)

    def test_v1_completed_condition_true_or_false_is_terminal(self):
        for finished in ("True", "False"):
            with self.subTest(finished=finished):
                self.assertTrue(rebind.execution_terminal({
                    "metadata": {"name": "hunter-hk-daily-old"},
                    "status": {"conditions": [
                        {"type": "Completed", "status": finished},
                    ]},
                }))

    def test_v2_completed_condition_and_completion_enum_are_terminal(self):
        for state in ("CONDITION_SUCCEEDED", "CONDITION_FAILED"):
            self.assertTrue(rebind.execution_terminal({
                "conditions": [{"type": "Completed", "state": state}],
            }))
        for state in ("EXECUTION_SUCCEEDED", "EXECUTION_FAILED", "EXECUTION_CANCELLED"):
            self.assertTrue(rebind.execution_terminal({"completionStatus": state}))

    def test_pending_list_may_be_resolved_by_fresh_terminal_describe(self):
        before = job("HK", OLD)
        expected = job("HK", SHA)
        pending = [{"name": "hunter-hk-daily-kw9vv",
                    "completionStatus": "EXECUTION_PENDING"}]
        terminal_detail = {
            "metadata": {"name": "hunter-hk-daily-kw9vv"},
            "status": {"conditions": [
                {"type": "Completed", "status": "False"}]},
        }
        calls = []
        def response(*args):
            calls.append(args)
            if "list" in args:
                return __import__("json").dumps(pending)
            self.assertEqual(args[:4], ("run", "jobs", "executions", "describe"))
            self.assertEqual(args[4], "hunter-hk-daily-kw9vv")
            return __import__("json").dumps(terminal_detail)

        with patch.object(rebind, "describe", side_effect=[
                job("US", SHA), before, copy.deepcopy(before), expected]), \
             patch.object(rebind, "gcloud", side_effect=response), \
             patch.object(rebind.subprocess, "run") as update:
            rebind.main(SHA)
            update.assert_called_once()
            self.assertEqual(len([a for a in calls if "describe" in a]), 2)

    def test_pending_execution_detail_never_allows_update(self):
        pending = [{"name": "hunter-hk-daily-kw9vv",
                    "completionStatus": "EXECUTION_PENDING"}]
        def response(*args):
            if "list" in args:
                return __import__("json").dumps(pending)
            return __import__("json").dumps(pending[0])
        with patch.object(rebind, "describe", side_effect=[
                job("US", SHA), job("HK", OLD)]), \
             patch.object(rebind, "gcloud", side_effect=response), \
             patch.object(rebind.subprocess, "run") as update:
            with self.assertRaisesRegex(RuntimeError, "NOT_PROVEN_TERMINAL"):
                rebind.main(SHA)
            update.assert_not_called()

    def test_already_aligned_skips_update_and_new_execution(self):
        aligned = job("HK", SHA)
        with patch.object(rebind, "describe", side_effect=[job("US", SHA), aligned]), \
             patch.object(rebind, "gcloud") as reads, \
             patch.object(rebind.subprocess, "run") as update:
            rebind.main(SHA)
            reads.assert_not_called()
            update.assert_not_called()

    def test_concurrent_job_drift_fails_before_update(self):
        before = job("HK", OLD)
        drift = copy.deepcopy(before)
        rebind.only_container(drift)["resources"]["limits"]["cpu"] = "2"
        with patch.object(rebind, "describe", side_effect=[job("US", SHA), before, drift]), \
             patch.object(rebind, "gcloud", return_value="[]"), \
             patch.object(rebind.subprocess, "run") as update:
            with self.assertRaisesRegex(RuntimeError, "CONCURRENT_JOB_CHANGE"):
                rebind.main(SHA)
            update.assert_not_called()

    def test_source_image_must_be_approved_and_actual_us_image(self):
        with patch.object(rebind, "describe", side_effect=[job("US", OLD), job("HK", OLD)]), \
             patch.object(rebind, "gcloud") as reads, \
             patch.object(rebind.subprocess, "run") as update:
            with self.assertRaisesRegex(RuntimeError, "US_TESTED_IMAGE_NOT_DEPLOYED"):
                rebind.main(SHA)
            reads.assert_not_called()
            update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
