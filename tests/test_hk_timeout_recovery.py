import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "hk_recovery", Path(__file__).resolve().parents[1] /
    "cloudrun/recover-hk-timeout-20261006.py")
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def execution(name, status="Unknown", running=0):
    return {"metadata": {"name": name}, "status": {
        "runningCount": running,
        "conditions": [{"type": "Completed", "status": status}]}}


def job(image=recovery.IMAGE):
    return {"spec": {"template": {"spec": {"template": {"spec": {
        "containers": [{"image": image, "args": ["--mode", "auto", "--market", "HK"],
                        "env": [{"name": k, "value": v} for k, v in recovery.SETTINGS.items()]}]
    }}}}}}


class HkTimeoutRecoveryTests(unittest.TestCase):
    def run_main(self, gc):
        with patch.object(recovery, "gc", side_effect=gc) as mock, \
                patch.object(recovery.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            recovery.main()
        return mock.call_args_list

    def test_pending_is_active_and_blocks_replacement(self):
        pending = execution("hunter-hk-daily-other", running=0)
        self.assertEqual(recovery.state(pending), "ACTIVE")
        def gc(*args):
            if args[:3] == ("run", "jobs", "describe"):
                return job()
            if "list" in args:
                return [pending]
            if args[0] == "logging":
                return []
            self.assertNotIn("execute", args)
            self.assertNotIn("cancel", args)
            return {}
        self.run_main(gc)

    def test_wrong_image_stops_before_mutation(self):
        with patch.object(recovery, "gc", return_value=job("unexpected")) as mock:
            with self.assertRaisesRegex(RuntimeError, "EXPECTED_DEPLOYED_IMAGE"):
                recovery.main()
        self.assertEqual(mock.call_count, 1)

    def test_unconfirmed_cancellation_never_starts_replacement(self):
        old = execution(recovery.OLD, running=1)
        def gc(*args):
            if args[:3] == ("run", "jobs", "describe"):
                return job()
            if "list" in args:
                return [old]
            if "describe" in args:
                return old
            self.assertNotIn("execute", args)
            return {}
        with self.assertRaisesRegex(RuntimeError, "CANCEL_NOT_CONFIRMED"):
            self.run_main(gc)

    def test_cancellation_then_one_replacement_and_no_us_mutation(self):
        old, ended = execution(recovery.OLD, running=1), execution(recovery.OLD, "False")
        own = execution("hunter-hk-daily-new", running=1)
        lists = iter([[old], [ended], [own]])
        def gc(*args):
            if args[:3] == ("run", "jobs", "describe"):
                return job()
            if "list" in args:
                return next(lists)
            if "describe" in args:
                return ended
            if "execute" in args:
                return own
            if args[0] == "logging":
                return []
            return {}
        calls = self.run_main(gc)
        mutations = [c.args for c in calls if c.args[0] == "run" and
                     any(action in c.args for action in ("update", "execute", "cancel"))]
        self.assertEqual(sum("execute" in args for args in mutations), 1)
        self.assertTrue(all("hunter-us-daily" not in args for args in mutations))
        self.assertLess(next(i for i, c in enumerate(calls) if "cancel" in c.args),
                        next(i for i, c in enumerate(calls) if "execute" in c.args))

    def test_observed_launch_race_cancels_only_own_new_execution(self):
        own = execution("hunter-hk-daily-new", running=1)
        other = execution("hunter-hk-daily-other", running=1)
        lists = iter([[], [], [own, other]])
        def gc(*args):
            if args[:3] == ("run", "jobs", "describe"):
                return job()
            if "list" in args:
                return next(lists)
            if "execute" in args:
                return own
            if args[0] == "logging":
                return []
            return {}
        calls = self.run_main(gc)
        cancels = [c.args for c in calls if "cancel" in c.args]
        self.assertEqual(len(cancels), 1)
        self.assertIn("hunter-hk-daily-new", cancels[0])
        self.assertNotIn("hunter-hk-daily-other", cancels[0])


if __name__ == "__main__":
    unittest.main()
