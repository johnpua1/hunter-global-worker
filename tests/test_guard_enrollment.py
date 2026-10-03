import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "enrollment", Path(__file__).resolve().parents[1] / "cloudrun/enroll-production-guard.py")
enrollment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enrollment)


def document(job):
    sha, scope, version = enrollment.PREVIOUS[job]
    args = (["/app/maintenance.py"] if scope == "MAINT" else
            ["--mode", "monthly"] if scope == "MONTH" else
            ["--mode", "auto", "--market", scope])
    c = {"image": "us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:" + sha,
         "args": args, "command": ["python"] if scope == "MAINT" else [],
         "env": [{"name": "HUNTER_SOURCE_SHA", "value": sha},
                 {"name": "APPS_SCRIPT_SHARED_KEY", "valueFrom": {"secretKeyRef": {
                     "name": "APPS_SCRIPT_SHARED_KEY_" + scope, "key": version}}}]}
    account = "hunter-monthly" if scope == "MONTH" else job
    task = {"serviceAccountName": account + "@rgs-hunter-global.iam.gserviceaccount.com",
            "containers": [c]}
    return {"spec": {"template": {"spec": {
        "taskCount": 1, "parallelism": 1, "template": {"spec": task}}}}}


class GuardEnrollmentTests(unittest.TestCase):
    def test_config_reader_grants_only_execution_get_on_each_own_job(self):
        calls = []
        def gc(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("iam", "roles", "list"):
                return []
            if args[:3] == ("iam", "roles", "create"):
                return {"includedPermissions": ["run.executions.get"]}
            return {}
        with patch.object(enrollment, "gc", side_effect=gc):
            enrollment.prepare_config_reader(["hunter-us-daily", "hunter-monthly-v2"])
        self.assertIn("--permissions=run.executions.get", calls[1])
        self.assertEqual(calls[2][3], "hunter-us-daily")
        self.assertIn("--member=serviceAccount:hunter-us-daily@rgs-hunter-global.iam.gserviceaccount.com", calls[2])
        self.assertIn("--member=serviceAccount:hunter-monthly@rgs-hunter-global.iam.gserviceaccount.com", calls[3])

    def test_probe_rejects_success_that_hides_a_config_block_on_retry(self):
        job = "hunter-us-daily"
        replies = [
            {"metadata": {"name": job + "-test"}},
            [{"textPayload": "HUNTER_CONFIG_OK worker=" + job + " myt=test sha256=" + "a" * 64}],
            [{"textPayload": "HUNTER_CONFIG_BLOCKED reason=ValueError:HASH_MISMATCH"}],
        ]
        with patch.object(enrollment, "gc", side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, "PROBE_RETRIED_AFTER_CONFIG_BLOCK"):
                enrollment.probe(job, "VERIFY")

    def test_resume_uses_built_release_and_does_not_touch_other_jobs(self):
        release = "b" * 40
        target = "hunter-hk-daily"
        calls = []
        def gc(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("run", "jobs", "describe"):
                return copy.deepcopy(document(args[3]))
            return {}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(enrollment, "gc", side_effect=gc), \
             patch.object(enrollment, "probe", side_effect=RuntimeError("PROBE_FAILED")), \
             patch.object(enrollment.subprocess, "check_output") as git, \
             patch.object(enrollment.tempfile, "mkdtemp", return_value=folder):
            with self.assertRaisesRegex(RuntimeError, "PROBE_FAILED"):
                enrollment.main(release=release, jobs=[target])
        git.assert_not_called()
        self.assertTrue(all(call[3] == target for call in calls))
        self.assertIn("--image=us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:" + release,
                      calls[1])

    def test_preflight_accepts_known_roles_and_rejects_unpinned_secret_and_probe(self):
        for job in enrollment.PREVIOUS:
            d = document(job)
            enrollment.validate_previous(job, d, "a" * 40)
            c = enrollment.parts(d)[2]
            c["env"][-1]["valueFrom"]["secretKeyRef"]["key"] = "latest"
            with self.assertRaisesRegex(RuntimeError, "SECRET_REFERENCE_MISMATCH"):
                enrollment.validate_previous(job, d, "a" * 40)
            d = document(job)
            enrollment.parts(d)[2]["env"].append({"name": "HUNTER_CONFIG_PROBE", "value": "ENROLL"})
            with self.assertRaisesRegex(RuntimeError, "PERSISTENT_PROBE_FORBIDDEN"):
                enrollment.validate_previous(job, d, "a" * 40)

    def test_failed_probe_restores_previous_image_and_removes_new_hash(self):
        updates = []
        def gc(*args, **kwargs):
            if args[:3] == ("run", "jobs", "describe"):
                return copy.deepcopy(document(args[3]))
            updates.append(args)
            return {}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(enrollment, "gc", side_effect=gc), \
             patch.object(enrollment, "probe", side_effect=RuntimeError("PROBE_FAILED")), \
             patch.object(enrollment.subprocess, "check_output", return_value="a" * 40), \
             patch.object(enrollment.tempfile, "mkdtemp", return_value=folder):
            with self.assertRaisesRegex(RuntimeError, "PROBE_FAILED"):
                enrollment.main()
        self.assertEqual(len(updates), 2)
        self.assertIn("--image=" + enrollment.parts(document("hunter-us-daily"))[2]["image"], updates[-1])
        self.assertIn("--remove-env-vars=HUNTER_CONFIG_SHA256", updates[-1])


if __name__ == "__main__":
    unittest.main()
