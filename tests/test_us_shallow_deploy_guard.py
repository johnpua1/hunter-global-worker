"""Verify the US-only Cloud Shell deploy guard handles --depth 1 safely."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

P = Path(__file__).resolve().parents[1] / "cloudrun/deploy-us-phase2-fix-20261015.py"
spec = importlib.util.spec_from_file_location("us_shallow_guard", P)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SHA = "a" * 40


class USShallowGuardTests(unittest.TestCase):
    def test_shallow_clone_fetches_before_ancestor_proof(self):
        calls = []
        def output(args, text=True):
            calls.append(("read", args))
            if "--is-shallow-repository" in args:
                return "true\n"
            return SHA + "\n"
        def command(args, **kwargs):
            calls.append(("run", args))
            return Mock(returncode=0)
        with patch.object(mod.subprocess, "check_output", side_effect=output), \
             patch.object(mod.subprocess, "run", side_effect=command):
            mod.verify_checkout(SHA)
        runs = [args for kind,args in calls if kind=="run"]
        self.assertEqual(len(runs), 2)
        self.assertIn("--unshallow", runs[0])
        self.assertIn("--is-ancestor", runs[1])
        self.assertEqual(sum(1 for kind,args in calls if kind=="read" and "HEAD" in args), 2)

    def test_full_checkout_preserves_no_fetch(self):
        def output(args, text=True):
            return "false\n" if "--is-shallow-repository" in args else SHA+"\n"
        with patch.object(mod.subprocess, "check_output", side_effect=output), \
             patch.object(mod.subprocess, "run", return_value=Mock(returncode=0)) as cmd:
            mod.verify_checkout(SHA)
        self.assertEqual(cmd.call_count, 1)
        self.assertIn("--is-ancestor", cmd.call_args.args[0])

    def test_required_ancestor_is_still_mandatory(self):
        def output(args, text=True):
            return "false" if "--is-shallow-repository" in args else SHA
        with patch.object(mod.subprocess, "check_output", side_effect=output), \
             patch.object(mod.subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "ANCESTRY_UNVERIFIED"):
                mod.verify_checkout(SHA)

    def test_failed_history_fetch_never_bypasses_guard(self):
        def output(args, text=True):
            return "true" if "--is-shallow-repository" in args else SHA
        with patch.object(mod.subprocess, "check_output", side_effect=output), \
             patch.object(mod.subprocess, "run",
                          side_effect=subprocess.CalledProcessError(1, "git fetch")) as cmd:
            with self.assertRaises(subprocess.CalledProcessError):
                mod.verify_checkout(SHA)
        self.assertEqual(cmd.call_count, 1)


if __name__=="__main__":
    unittest.main()
