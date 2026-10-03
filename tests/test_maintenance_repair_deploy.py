import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("maint_deploy", Path(__file__).resolve().parents[1] / "cloudrun/deploy-maintenance-repair.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class MaintenanceRepairDeployTests(unittest.TestCase):
    def test_running_job_waits_then_proceeds_only_after_idle(self):
        check = SimpleNamespace(ensure_idle=Mock(side_effect=[RuntimeError("JOB_STILL_RUNNING:maint"), None]),
                                command=Mock(return_value=json.dumps([])))
        with patch.object(deploy.time, "sleep") as sleep, patch.object(deploy, "stamp"):
            deploy.wait_idle(check, True)
        sleep.assert_called_once_with(60)
        self.assertEqual(check.ensure_idle.call_count, 2)

    def test_permission_errors_are_not_treated_as_running_jobs(self):
        check = SimpleNamespace(ensure_idle=Mock(side_effect=RuntimeError("PERMISSION_DENIED")))
        with patch.object(deploy.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "PERMISSION_DENIED"):
                deploy.wait_idle(check, True)
        sleep.assert_not_called()

    def test_wait_limit_stops_before_deployment(self):
        check = SimpleNamespace(ensure_idle=Mock(side_effect=RuntimeError("JOB_STILL_RUNNING:maint")))
        with patch.object(deploy.time, "monotonic", side_effect=[0, 7200]), patch.object(deploy, "stamp"):
            with self.assertRaisesRegex(RuntimeError, "IDLE_WAIT_LIMIT"):
                deploy.wait_idle(check, True)


if __name__ == "__main__":
    unittest.main()
