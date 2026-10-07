import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from production_guard import check_at_start, fingerprint, safe_error_summary


class ProductionGuardTests(unittest.TestCase):
    def test_monthly_job_checks_the_fingerprint_normally(self):
        env = {"CLOUD_RUN_JOB": "hunter-monthly-v2", "CLOUD_RUN_TASK_COUNT": "1",
               "HUNTER_SOURCE_SHA": "a" * 40}
        argv = ["/app/runner.py", "--mode", "monthly"]
        env["HUNTER_CONFIG_SHA256"] = fingerprint(
            job=env["CLOUD_RUN_JOB"], environ=env, argv=argv,
            service_account="sa@example.test", cpu_limit="100000", memory_limit="100000")
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", argv), \
             patch("production_guard._service_account_email", return_value="sa@example.test"), \
             patch("production_guard._read_limit", return_value="100000"), \
             self.assertLogs("hunter.guard", level=logging.INFO) as logs:
            check_at_start()
        self.assertIn("HUNTER_CONFIG_OK worker=hunter-monthly-v2", logs.output[0])
        self.assertNotIn("HUNTER_CONFIG_DRIFT", " ".join(logs.output))

    def test_fingerprint_changes_with_identity_and_secret_without_exposing_secret(self):
        config = dict(job="hunter-us-daily", environ={
            "APPS_SCRIPT_SHARED_KEY": "sensitive-value", "FETCH_WORKERS": "10",
            "CLOUD_RUN_TASK_COUNT": "1"}, argv=["/app/runner.py", "--mode", "auto"],
            service_account="hunter-jobs@example.test", cpu_limit="100000 100000",
            memory_limit="536870912")
        original = fingerprint(**config)
        self.assertEqual(len(original), 64)
        self.assertNotIn("sensitive-value", original)
        self.assertNotEqual(original, fingerprint(**{**config, "service_account": "other@example.test"}))
        self.assertNotEqual(original, fingerprint(**{**config, "environ": {
            **config["environ"], "APPS_SCRIPT_SHARED_KEY": "rotated"}}))

    def test_mismatch_alerts_and_continues_without_logging_secret(self):
        env = {"CLOUD_RUN_JOB": "hunter-hk-daily", "CLOUD_RUN_TASK_COUNT": "1",
               "APPS_SCRIPT_SHARED_KEY": "sensitive-value", "HUNTER_CONFIG_SHA256": "0" * 64,
               "HUNTER_SOURCE_SHA": "a" * 40}
        with patch.dict(os.environ, env, clear=True), \
             patch("production_guard._service_account_email", return_value="sa@example.test"), \
             patch("production_guard._read_limit", return_value="100000"), \
             self.assertLogs("hunter.guard", level=logging.ERROR) as logs:
            self.assertIsNone(check_at_start())
        self.assertIn("HUNTER_CONFIG_DRIFT worker=hunter-hk-daily", logs.output[0])
        self.assertIn("myt=", logs.output[0])
        self.assertNotIn("sensitive-value", logs.output[0])

    def test_metadata_failure_alerts_without_stopping_job(self):
        with patch.dict(os.environ, {"CLOUD_RUN_JOB": "hunter-maintenance",
                                     "HUNTER_SOURCE_SHA": "a" * 40}, clear=True), \
             patch("production_guard._service_account_email", side_effect=TimeoutError()), \
             self.assertLogs("hunter.guard", level=logging.ERROR) as logs:
            self.assertIsNone(check_at_start())
        self.assertIn("reason=HASH_UNAVAILABLE:TimeoutError", logs.output[0])

    def test_failure_summary_never_logs_arbitrary_exception_text(self):
        self.assertEqual(safe_error_summary(RuntimeError("BRIDGE_AUTH_MISSING:detail")),
                         "RuntimeError:BRIDGE_AUTH_MISSING")
        self.assertEqual(safe_error_summary(ValueError("key=sensitive-value")),
                         "ValueError:UNCLASSIFIED")


if __name__ == "__main__":
    unittest.main()
