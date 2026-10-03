import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("lock_deploy", Path(__file__).resolve().parents[1] / "cloudrun/deploy-maintenance-lock.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class LockDeployTests(unittest.TestCase):
    def test_only_reviewed_runtime_files_are_allowed(self):
        files = "hunter-global/maintenance.py\nhunter-global/maintenance_lock.py\n"
        helper = SimpleNamespace(command=Mock(side_effect=["a" * 40, files, ""]))
        self.assertEqual(deploy.reviewed_release(helper), "a" * 40)
        helper.command = Mock(side_effect=["a" * 40, files + "hunter-global/repair.py\n"])
        with self.assertRaisesRegex(RuntimeError, "UNREVIEWED_MUTEX_RUNTIME_CHANGE"):
            deploy.reviewed_release(helper)

    def run_flow(self, *, busy=False, probe_error=False):
        events = []
        before = {"old": True}
        guard = SimpleNamespace(REGION="us-central1", PROJECT="rgs-hunter-global", PREVIOUS={},
            validate_previous=Mock(), parts=Mock(return_value=(None, None, {"image": "old"}, {})),
            main=Mock(side_effect=lambda **kw: events.append("deploy")), probe=Mock())
        def gc(*args):
            if args[:3] == ("run", "jobs", "describe"):
                return before
            events.append("start")
            return {"metadata": {"name": "hunter-maintenance-new"}}
        guard.gc = Mock(side_effect=gc)
        def idle(*args):
            events.append("idle")
            if busy:
                raise RuntimeError("JOB_STILL_RUNNING:old")
        repair = SimpleNamespace(ensure_image=Mock(side_effect=lambda _: events.append("build")),
                                 wait_idle=Mock(side_effect=idle), stamp=Mock())
        def probe(_):
            events.append("probe")
            if probe_error:
                raise RuntimeError("PROBE_FAILED")
        with patch.object(deploy, "module", side_effect=[object(), repair, guard]), \
             patch.object(deploy, "reviewed_release", return_value="a" * 40), \
             patch.object(deploy, "verify_lock_probe", side_effect=probe), \
             patch.object(deploy, "rollback", side_effect=lambda *a: events.append("rollback")), \
             patch("sys.argv", ["deploy", "--wait-idle"]):
            if busy or probe_error:
                with self.assertRaises(RuntimeError):
                    deploy.main()
            else:
                deploy.main()
        return events

    def test_deploy_and_resume_are_separated_by_idle_and_probe_checks(self):
        self.assertEqual(self.run_flow(), ["build", "idle", "deploy", "idle", "probe", "idle", "start"])

    def test_running_job_blocks_all_production_changes(self):
        self.assertEqual(self.run_flow(busy=True), ["build", "idle"])

    def test_failed_lock_probe_rolls_back_without_business_launch(self):
        self.assertEqual(self.run_flow(probe_error=True), ["build", "idle", "deploy", "idle", "probe", "rollback"])


if __name__ == "__main__":
    unittest.main()
