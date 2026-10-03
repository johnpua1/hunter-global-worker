import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from production_guard import check_at_start, fingerprint, resource_limits, safe_error_summary


class ProductionGuardTests(unittest.TestCase):
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

    def config(self, job="hunter-us-daily"):
        from production_guard import JOB_ARGS
        env = {"CLOUD_RUN_JOB": job, "CLOUD_RUN_TASK_COUNT": "1",
               "HUNTER_ACTIONS_CUTOVER": "CONFIRMED", "APPS_SCRIPT_SHARED_KEY": "sensitive-value",
               "APPS_SCRIPT_WEBAPP_URL": "https://script.google.com/macros/s/test/exec",
               "HUNTER_CONFIG_SHA256": "0" * 64, "HUNTER_SOURCE_SHA": "a" * 40}
        account = ("hunter-monthly" if job == "hunter-monthly-v2" else job) + "@rgs-hunter-global.iam.gserviceaccount.com"
        return env, JOB_ARGS[job], account

    def test_mismatch_blocks_without_logging_secret(self):
        env, argv, account = self.config()
        with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv), \
             patch("production_guard._service_account_email", return_value=account), \
             patch("production_guard.resource_limits", return_value=("100000", "100000")), \
             self.assertLogs("hunter.guard", level=logging.ERROR) as logs:
            with self.assertRaisesRegex(RuntimeError, "HUNTER_CONFIG_BLOCKED"):
                check_at_start()
        self.assertIn("HASH_MISMATCH", logs.output[0])
        self.assertNotIn("sensitive-value", str(logs.output))

    def test_metadata_failure_blocks_instead_of_skipping_verification(self):
        env, argv, _ = self.config()
        with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv), \
             patch("production_guard._service_account_email", side_effect=TimeoutError()):
            with self.assertRaisesRegex(RuntimeError, "HUNTER_CONFIG_BLOCKED"):
                check_at_start()

    def test_all_four_roles_enroll_then_verify_and_exit_before_drive(self):
        from production_guard import JOB_ARGS
        import runner
        import maintenance
        for job in JOB_ARGS:
            env, argv, account = self.config(job)
            expected = fingerprint(job=job, environ=env, argv=argv, service_account=account,
                                   cpu_limit="100000", memory_limit="100000")
            env["HUNTER_CONFIG_SHA256"] = expected
            for mode in ("ENROLL", "VERIFY"):
                with self.subTest(job=job, mode=mode), \
                     patch.dict(os.environ, {**env, "HUNTER_CONFIG_PROBE": mode}, clear=True), \
                     patch.object(sys, "argv", argv), \
                     patch("production_guard._service_account_email", return_value=account), \
                     patch("production_guard.resource_limits", return_value=("100000", "100000")), \
                     patch("runner.Drive") as runner_drive, patch("maintenance.Drive") as maint_drive:
                    with self.assertRaises(SystemExit) as outcome:
                        (maintenance.main if job == "hunter-maintenance" else runner.main)()
                    self.assertEqual(outcome.exception.code, 0)
                    runner_drive.assert_not_called()
                    maint_drive.assert_not_called()
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv), \
                 patch("production_guard._service_account_email", return_value=account), \
                 patch("production_guard.resource_limits", return_value=("100000", "100000")):
                self.assertIsNone(check_at_start())

    def test_enrollment_rejects_wrong_role_account_and_whitespace_secret(self):
        env, argv, account = self.config()
        for changed_env, changed_argv, changed_account in [
            ({"CLOUD_RUN_TASK_COUNT": "2"}, argv, account),
            ({"APPS_SCRIPT_SHARED_KEY": "sensitive-value\n"}, argv, account),
            ({}, argv + ["--as-of", "2026-10-01"], account),
            ({}, argv, "other@example.test"),
        ]:
            with patch.dict(os.environ, {**env, **changed_env, "HUNTER_CONFIG_PROBE": "ENROLL"}, clear=True), \
                 patch.object(sys, "argv", changed_argv), \
                 patch("production_guard._service_account_email", return_value=changed_account), \
                 patch("production_guard.resource_limits", return_value=("100000", "100000")):
                with self.assertRaisesRegex(RuntimeError, "HUNTER_CONFIG_BLOCKED"):
                    check_at_start()

    def test_resource_limits_observed_cloud_run_v1_and_v2_are_equivalent(self):
        # Values and combined controller mount from the 2026-10-03 MYT probe.
        layouts = [
            {"cpu,cpuacct/cpu.cfs_quota_us": "98600",
             "cpu,cpuacct/cpu.cfs_period_us": "100000",
             "memory/memory.limit_in_bytes": "536870912"},
            {"cpu/cpu.cfs_quota_us": "98600",
             "cpu/cpu.cfs_period_us": "100000",
             "memory/memory.limit_in_bytes": "536870912"},
            {"cpuacct,cpu/cpu.cfs_quota_us": "98600",
             "cpuacct,cpu/cpu.cfs_period_us": "100000",
             "memory/memory.limit_in_bytes": "536870912"},
            {"cpu.max": "98600 100000", "memory.max": "536870912"},
            {"unified/cpu.max": "98600 100000", "unified/memory.max": "536870912"},
        ]
        for layout in layouts:
            files = {"/sys/fs/cgroup/" + k: v for k, v in layout.items()}
            with self.subTest(layout=layout), patch("production_guard._read_limit",
                    side_effect=lambda path: files.get(path, "UNAVAILABLE")):
                self.assertEqual(resource_limits(), ("98600 100000", "536870912"))

    def test_missing_or_invalid_resource_limits_block(self):
        for files in ({}, {"cpu.max": "max 100000", "memory.max": "536870912"},
                      {"cpu.max": "98600 0", "memory.max": "536870912"},
                      {"cpu.max": "garbage", "memory.max": "536870912"},
                      {"cpu.max": "98600 100000", "memory.max": "max"},
                      {"cpu/cpu.cfs_quota_us": "98600",
                       "cpu/cpu.cfs_period_us": "100000"}):
            paths = {"/sys/fs/cgroup/" + k: v for k, v in files.items()}
            with self.subTest(files=files), patch("production_guard._read_limit",
                    side_effect=lambda path: paths.get(path, "UNAVAILABLE")):
                with self.assertRaisesRegex(ValueError, "RESOURCE_LIMIT_"):
                    resource_limits()

    def test_failure_summary_never_logs_arbitrary_exception_text(self):
        self.assertEqual(safe_error_summary(RuntimeError("BRIDGE_AUTH_MISSING:detail")),
                         "RuntimeError:BRIDGE_AUTH_MISSING")
        self.assertEqual(safe_error_summary(ValueError("key=sensitive-value")),
                         "ValueError:UNCLASSIFIED")


if __name__ == "__main__":
    unittest.main()
