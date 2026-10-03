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

    def execution_doc(self, cpu="1", memory="512Mi"):
        return {"name": "projects/rgs-hunter-global/locations/us-central1/jobs/hunter-us-daily/executions/hunter-us-daily-test",
                "taskCount": 1, "parallelism": 1,
                "template": {"containers": [{"resources": {"limits": {"cpu": cpu, "memory": memory}}}]}}

    def test_declared_limits_ignore_cgroup_and_equivalent_units_match(self):
        for cpu, memory in [("1", "512Mi"), ("1000m", "536870912"), ("1.0", "0.5Gi")]:
            with patch.dict(os.environ, {"CLOUD_RUN_JOB": "hunter-us-daily",
                    "CLOUD_RUN_EXECUTION": "hunter-us-daily-test"}, clear=True), \
                 patch("production_guard._get_json", side_effect=[
                     {"access_token": "private-token"}, self.execution_doc(cpu, memory)]) as get, \
                 patch("pathlib.Path.read_text", side_effect=AssertionError("CGROUP_MUST_NOT_BE_READ")):
                self.assertEqual(resource_limits(), ("1", "536870912"))
                self.assertIn("/jobs/hunter-us-daily/executions/hunter-us-daily-test", get.call_args.args[0])

    def test_invalid_execution_and_limits_fail_closed(self):
        docs = [self.execution_doc("max"), self.execution_doc(memory="max"),
                {**self.execution_doc(), "name": "wrong"},
                {**self.execution_doc(), "taskCount": 2},
                {**self.execution_doc(), "template": {"containers": []}}]
        for doc in docs:
            with patch.dict(os.environ, {"CLOUD_RUN_JOB": "hunter-us-daily",
                    "CLOUD_RUN_EXECUTION": "hunter-us-daily-test"}, clear=True), \
                 patch("production_guard._get_json", side_effect=[{"access_token": "private-token"}, doc]):
                with self.assertRaises(ValueError):
                    resource_limits()

    def test_cpu_and_memory_drift_still_change_fingerprint(self):
        env, argv, account = self.config()
        data = dict(job="hunter-us-daily", environ=env, argv=argv,
                    service_account=account, cpu_limit="1", memory_limit="536870912")
        self.assertNotEqual(fingerprint(**data), fingerprint(**{**data, "cpu_limit": "2"}))
        self.assertNotEqual(fingerprint(**data), fingerprint(**{**data, "memory_limit": "1073741824"}))

    def test_api_permission_failure_blocks_without_printing_token(self):
        from production_guard import _get_json
        from unittest.mock import Mock
        with patch("production_guard.requests.get", return_value=Mock(status_code=403)):
            with self.assertRaisesRegex(ValueError, "^EXECUTION_CONFIG_HTTP_403$"):
                _get_json("https://run.googleapis.com/v2/test",
                          {"Authorization": "Bearer private-token"}, "EXECUTION_CONFIG")

    def test_failure_summary_never_logs_arbitrary_exception_text(self):
        self.assertEqual(safe_error_summary(RuntimeError("BRIDGE_AUTH_MISSING:detail")),
                         "RuntimeError:BRIDGE_AUTH_MISSING")
        self.assertEqual(safe_error_summary(ValueError("key=sensitive-value")),
                         "ValueError:UNCLASSIFIED")


if __name__ == "__main__":
    unittest.main()
