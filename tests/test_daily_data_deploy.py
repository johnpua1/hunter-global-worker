import copy
import importlib.util
import pathlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("daily_deploy", ROOT / "cloudrun/deploy-daily-data-only.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
SHA = "a" * 40


def job():
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{
        "image": "old", "command": ["python", "/app/runner.py"], "args": ["--mode", "auto"],
        "resources": {"limits": {"memory": "1Gi"}}, "env": [
            {"name": "HUNTER_ACTIONS_CUTOVER", "value": "CONFIRMED"},
            {"name": "APPS_SCRIPT_WEBAPP_URL", "valueFrom": {"secretKeyRef": {"name": "url", "key": "latest"}}},
            {"name": "APPS_SCRIPT_SHARED_KEY", "valueFrom": {"secretKeyRef": {"name": "key", "key": "latest"}}}]}]}}}}}}


class DeployTests(unittest.TestCase):
    def test_new_image_entrypoint_and_secrets_preserved(self):
        original = d.container(job())
        new = d.expected_container(original, "HK", SHA)
        self.assertNotIn("command", new)
        self.assertEqual(new["args"], ["--mode", "daily-data", "--market", "HK"])
        self.assertEqual(new["resources"], original["resources"])
        for name in ("APPS_SCRIPT_WEBAPP_URL", "APPS_SCRIPT_SHARED_KEY"):
            self.assertEqual(next(x for x in new["env"] if x["name"] == name), next(x for x in original["env"] if x["name"] == name))
        self.assertEqual(dict((x["name"], x.get("value")) for x in new["env"])["HUNTER_PHASE2_ENABLED"], "0")

    def test_template_failure_never_cancels_or_launches(self):
        calls = []
        def fake_gc(*args):
            calls.append(args)
            if args[:3] == ("run", "jobs", "describe"):
                return job()
            return {}
        with patch.object(d, "gc", fake_gc), patch.object(d.subprocess, "check_output", return_value=SHA), patch.object(d.subprocess, "run"):
            with self.assertRaisesRegex(RuntimeError, "TEMPLATE_MISMATCH"):
                d.deploy(SHA)
        self.assertFalse(any("cancel" in c or "execute" in c for c in calls))

    def test_auth_failure_prevents_build_or_changes(self):
        with patch.object(d, "gc", side_effect=RuntimeError("AUTH_REQUIRED")), patch.object(d.subprocess, "check_output", return_value=SHA), patch.object(d.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "AUTH_REQUIRED"):
                d.deploy(SHA)
            run.assert_not_called()

    def test_pending_and_terminal_executions(self):
        self.assertTrue(d.active({"status": {"conditions": [{"type": "Completed", "status": "Unknown"}]}}))
        self.assertFalse(d.active({"status": {"conditions": [{"type": "Completed", "status": "False"}]}}))

    def test_cutover_cancels_old_before_data_launch_and_keeps_schedule(self):
        docs = {m: job() for m in ("US", "HK")}
        cancelled, launched, calls = set(), [], []
        def fake_gc(*args):
            calls.append(args)
            if args[:3] == ("run", "jobs", "describe"):
                return copy.deepcopy(docs["US" if "us" in args[3] else "HK"])
            if args[:3] == ("run", "jobs", "update"):
                m = "US" if "us" in args[3] else "HK"
                c = d.container(docs[m])
                new = d.expected_container(c, m, SHA)
                c.clear(); c.update(new)
                self.assertIn("--command=", args)
                return {}
            if args[:4] == ("run", "jobs", "executions", "list"):
                m = "us" if "us" in args[4] else "hk"
                n = "hunter-" + m + "-daily-old"
                return [] if n in cancelled else [{"metadata": {"name": n}, "status": {}}]
            if args[:4] == ("run", "jobs", "executions", "describe"):
                return {"status": {"completionTime": "now"} if args[4] in cancelled else {},
                        "spec": {"template": {"spec": {"containers": [{"image": "old", "env": []}]}}}}
            if args[:4] == ("run", "jobs", "executions", "cancel"):
                cancelled.add(args[4]); return {}
            if args[:3] == ("run", "jobs", "execute"):
                self.assertEqual(len(cancelled), 2)
                launched.append(args[3]); return {}
            raise AssertionError(args)
        with patch.object(d, "gc", fake_gc), patch.object(d.subprocess, "check_output", return_value=SHA), patch.object(d.subprocess, "run"):
            d.deploy(SHA)
        self.assertEqual(launched, ["hunter-us-daily", "hunter-hk-daily"])
        self.assertTrue(all(c[0] != "scheduler" for c in calls))


if __name__ == "__main__":
    unittest.main()
