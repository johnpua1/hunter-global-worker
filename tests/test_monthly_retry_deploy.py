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
    def test_timezone_migration_preserves_all_other_manifest_fields(self):
        manifest = {"timeZone": "Asia/Singapore", "oauthScopes": ["scope"],
                    "runtimeVersion": "V8", "webapp": {"executeAs": "USER_DEPLOYING"}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "appsscript.json"
            path.write_text(json.dumps(manifest))
            self.assertEqual(deploy.patch_myt_manifest(root), "Asia/Singapore")
            expected = dict(manifest, timeZone="Asia/Kuala_Lumpur")
            self.assertEqual(json.loads(path.read_text()), expected)
            unchanged = path.read_bytes()
            self.assertEqual(deploy.patch_myt_manifest(root), "Asia/Kuala_Lumpur")
            self.assertEqual(path.read_bytes(), unchanged)
            manifest["timeZone"] = "America/New_York"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, "UNREVIEWED_SCRIPT_TIMEZONE"):
                deploy.patch_myt_manifest(root)
            self.assertEqual(json.loads(path.read_text()), manifest)

    def test_before_accepts_observed_timezone_after_requires_myt(self):
        doc = {"ok": True, "monthly": {"count": 1, "expectedMonth": "2026-11"},
               "daily": {"timeZone": "Asia/Singapore", "counts": {"dailyUS": 1, "dailyHK": 1},
                         "triggers": [{"triggerId": "us"}, {"triggerId": "hk"}]}}
        before = deploy.verify_launch_triggers(doc, "Asia/Singapore")
        expected = deploy.expected_myt_triggers(before)
        self.assertEqual(before["daily"]["timeZone"], "Asia/Kuala_Lumpur")
        self.assertEqual(deploy.verify_launch_triggers(doc, "Asia/Kuala_Lumpur"), expected)
        doc["daily"]["timeZone"] = "America/New_York"
        with self.assertRaisesRegex(RuntimeError, "TIMEZONE_READBACK_FAILED"):
            deploy.verify_launch_triggers(doc, "Asia/Kuala_Lumpur")
        doc["daily"]["timeZone"] = "Asia/Kuala_Lumpur"
        after = deploy.verify_launch_triggers(doc, "Asia/Kuala_Lumpur")
        self.assertEqual(after, expected)
        doc["daily"]["triggers"][0]["triggerId"] = "replacement"
        self.assertNotEqual(deploy.verify_launch_triggers(doc, "Asia/Kuala_Lumpur"), expected)
        doc["daily"]["counts"]["dailyUS"] = 2
        with self.assertRaisesRegex(RuntimeError, "TRIGGER_COUNT_READBACK_FAILED"):
            deploy.verify_launch_triggers(doc, "Asia/Kuala_Lumpur")

    def test_source_readback_accepts_only_manifest_timezone_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / "appsscript.json"
            gateway = root / "Gateway.js"
            gateway.write_text("function run() { return 1; }\n")
            manifest.write_text('{"timeZone":"Asia/Kuala_Lumpur","oauthScopes":["one"]}')
            expected = deploy.source_files(root)
            manifest.write_text('{"timeZone":"Asia/Singapore","oauthScopes":["one"]}')
            self.assertEqual(deploy.source_files(root), expected)
            manifest.write_text('{"timeZone":"Asia/Hong_Kong","oauthScopes":["one"]}')
            self.assertNotEqual(deploy.source_files(root), expected)
            manifest.write_text('{"timeZone":"Asia/Singapore","oauthScopes":["two"]}')
            self.assertNotEqual(deploy.source_files(root), expected)
            manifest.write_text('{"timeZone":"Asia/Singapore","oauthScopes":["one"]}')
            gateway.write_text("function run() { return 2; }\n")
            self.assertNotEqual(deploy.source_files(root), expected)

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
