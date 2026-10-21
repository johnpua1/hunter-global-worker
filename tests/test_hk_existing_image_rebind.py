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
            {"name": "hk-active", "completionStatus": "EXECUTION_PENDING"},
            {"name": "hk-unknown", "conditions": [{"type": "Completed", "state": "UNKNOWN"}]},
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
