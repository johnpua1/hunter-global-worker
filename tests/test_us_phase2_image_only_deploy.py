import copy
import importlib.util
import pathlib
import unittest
from unittest.mock import patch

path = pathlib.Path(__file__).resolve().parents[1] / "cloudrun/deploy-us-phase2-fix-20261015.py"
spec = importlib.util.spec_from_file_location("phase2_us_release", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
SHA = "a" * 40

def job(sha="b" * 40):
    return {"spec": {"template": {"spec": {
        "taskCount": 1, "parallelism": 1,
        "template": {"spec": {
            "timeoutSeconds": 7200, "maxRetries": 0,
            "serviceAccountName": "hunter-us-daily@example.iam.gserviceaccount.com",
            "containers": [{"image": mod.IMAGE_ROOT + ":" + sha,
                "args": ["--mode", "auto", "--market", "US"],
                "env": [{"name": "HUNTER_SOURCE_SHA", "value": sha}],
                "resources": {"limits": {"cpu": "1", "memory": "2Gi"}}}]
        }}
    }}}}

class DeployTests(unittest.TestCase):
    def test_no_change_skips_deploy(self):
        with patch.object(mod, "require_no_active_us_execution"), patch.object(mod, "verify_checkout"), patch.object(mod, "read_job", return_value=job(SHA)), \
             patch.object(mod, "build") as build, patch.object(mod, "update_job") as update:
            mod.main(SHA)
            build.assert_not_called()
            update.assert_not_called()

    def test_update_only_image_and_source_sha(self):
        before = job()
        after = job(SHA)
        with patch.object(mod, "require_no_active_us_execution"), patch.object(mod, "verify_checkout"), \
             patch.object(mod, "read_job", side_effect=[before, before, after]), \
             patch.object(mod, "build") as build, patch.object(mod, "update_job") as update:
            mod.main(SHA)
            build.assert_called_once_with(SHA)
            update.assert_called_once_with(mod.IMAGE_ROOT + ":" + SHA, SHA)
        self.assertEqual(mod.normalized_spec(after),
                         mod.expected_spec(before, mod.IMAGE_ROOT + ":" + SHA, SHA))

    def test_external_drift_aborts_before_update(self):
        before = job()
        drift = copy.deepcopy(before)
        mod.only_container(drift)["resources"]["limits"]["cpu"] = "2"
        with patch.object(mod, "require_no_active_us_execution"), patch.object(mod, "verify_checkout"), \
             patch.object(mod, "read_job", side_effect=[before, drift]), \
             patch.object(mod, "build"), patch.object(mod, "update_job") as update:
            with self.assertRaisesRegex(RuntimeError, "US_JOB_CHANGED_DURING_BUILD"):
                mod.main(SHA)
            update.assert_not_called()

    def test_build_failure_does_not_touch_cloud_run(self):
        with patch.object(mod, "require_no_active_us_execution"), patch.object(mod, "verify_checkout"), \
             patch.object(mod, "read_job", return_value=job()), \
             patch.object(mod, "build", side_effect=RuntimeError("BUILD_FAILED")), \
             patch.object(mod, "update_job") as update:
            with self.assertRaisesRegex(RuntimeError, "BUILD_FAILED"):
                mod.main(SHA)
            update.assert_not_called()

    def test_entrypoint_mismatch_aborts(self):
        bad = job()
        mod.only_container(bad)["args"] += ["--phase2-only"]
        with patch.object(mod, "require_no_active_us_execution"), patch.object(mod, "verify_checkout"), \
             patch.object(mod, "read_job", return_value=bad), \
             patch.object(mod, "update_job") as update:
            with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_US_JOB_CONFIGURATION"):
                mod.main(SHA)
            update.assert_not_called()

if __name__ == "__main__":
    unittest.main()
