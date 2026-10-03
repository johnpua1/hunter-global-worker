import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monthly_deploy", ROOT / "cloudrun/deploy-monthly-retry.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
OLD = "function monthlyV2() {\n  return 'old';\n}\n"
NEW = "function monthlyV2() {\n  return 'new';\n}\n"
TAIL = "\nfunction listMonthlyTrigger() {}\n"


class MonthlyDeployTests(unittest.TestCase):
    def test_launch_patch_preserves_monthly_manifest_and_other_functions(self):
        old_source = "\n".join("function " + name + "(job) {\n  return 'old';\n}\n"
                               for name in deploy.LAUNCH_FUNCTIONS)
        new_source = old_source.replace("'old'", "'new'")
        old, new = deploy.launch_functions(old_source), deploy.launch_functions(new_source)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gateway = root / "Gateway.js"
            gateway.write_text(old_source + OLD + TAIL)
            (root / "appsscript.json").write_text('{"timeZone":"Asia/Kuala_Lumpur"}')
            before = deploy.source_files(root)
            self.assertTrue(deploy.patch_launch(root, old, new))
            self.assertFalse(deploy.patch_launch(root, old, new))
            self.assertEqual(gateway.read_text(), new_source + OLD + TAIL)
            self.assertEqual(deploy.source_files(root)["appsscript.json"], before["appsscript.json"])
            gateway.write_text(old_source.replace("'old'", "'different'", 1))
            with self.assertRaisesRegex(RuntimeError, "LIVE_LAUNCH_DIFFERS"):
                deploy.patch_launch(root, old, new)

    def test_only_monthly_function_changes_and_rerun_is_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gateway = root / "Gateway.js"
            gateway.write_text("// preserve\n" + OLD + TAIL)
            (root / "Code.js").write_text("function unrelated() {}")
            (root / "appsscript.json").write_text('{"timeZone":"Asia/Kuala_Lumpur"}')
            before = deploy.source_files(root)
            self.assertTrue(deploy.patch_monthly(root, OLD, NEW))
            self.assertFalse(deploy.patch_monthly(root, OLD, NEW))
            after = deploy.source_files(root)
            self.assertEqual(after["Gateway.gs"], "// preserve\n" + NEW + TAIL)
            for name in ("Code.gs", "appsscript.json"):
                self.assertEqual(before[name], after[name])

    def test_unreviewed_or_duplicate_handler_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gateway = root / "Gateway.gs"
            gateway.write_text(OLD.replace("'old'", "'other'") + TAIL)
            with self.assertRaisesRegex(RuntimeError, "LIVE_MONTHLY_DIFFERS"):
                deploy.patch_monthly(root, OLD, NEW)
            gateway.write_text(OLD + TAIL)
            (root / "Other.js").write_text(OLD + TAIL)
            with self.assertRaisesRegex(RuntimeError, "HANDLER_NOT_UNIQUE"):
                deploy.patch_monthly(root, OLD, NEW)
            self.assertEqual(gateway.read_text(), OLD + TAIL)

    def test_live_job_blocks_deploy_but_terminal_failure_does_not(self):
        live = {"metadata": {"name": "hunter-hk-daily-running"}, "status": {
            "conditions": [{"type": "Completed", "status": "Unknown"}], "runningCount": 1}}
        with patch.object(deploy, "command", return_value=json.dumps([live])):
            with self.assertRaisesRegex(RuntimeError, "JOB_STILL_RUNNING"):
                deploy.ensure_idle()
        live["status"]["conditions"][0]["status"] = "False"
        with patch.object(deploy, "command", return_value=json.dumps([live])):
            deploy.ensure_idle()

    def test_only_exact_existing_deployment_is_accepted(self):
        with patch.object(deploy, "clasp", return_value=[{"deploymentId": "other", "versionNumber": 99}]):
            with self.assertRaisesRegex(RuntimeError, "EXISTING_VERSIONED_DEPLOYMENT_NOT_FOUND"):
                deploy.deployment_version("script", "expected", Path("/tmp"))

    def test_clone_uses_verified_clasp_341_positional_version(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(deploy, "clasp") as mocked:
            target = Path(folder) / "active"
            deploy.clone("script", target, 17)
            mocked.assert_called_once_with("clone-script", "script", "17", cwd=target)


if __name__ == "__main__":
    unittest.main()
