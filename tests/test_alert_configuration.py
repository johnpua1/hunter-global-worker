import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("alerts", Path(__file__).resolve().parents[1] / "cloudrun/configure-alerts.py")
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)


class AlertTests(unittest.TestCase):
    def test_policy_filters_cover_exact_jobs_and_exact_maintenance_scheduler(self):
        policies = alerts.definitions("channel", ["maintenance-schedule"])
        self.assertEqual(len(policies), 5)
        for job, policy in zip(alerts.JOBS, policies):
            query = policy["conditions"][0]["conditionMatchedLog"]["filter"]
            self.assertIn('resource.labels.job_name="' + job + '"', query)
            self.assertEqual(policy["notificationChannels"], ["channel"])
        control = policies[-1]["conditions"][0]["conditionMatchedLog"]["filter"]
        self.assertIn('resource.labels.job_id="maintenance-schedule"', control)
        self.assertIn('log_id("hunter-control")', control)

    def test_do_not_take_over_unmanaged_or_duplicate_policies(self):
        for rows in ([{"displayName": "mine"}],
                     [{"displayName": "mine", "userLabels": alerts.MANAGED}] * 2):
            with self.assertRaisesRegex(RuntimeError, "UNMANAGED_OR_DUPLICATE"):
                alerts.select_managed(rows, "mine")

    def test_readback_ignores_server_identifiers_but_detects_disabled_or_wrong_recipient(self):
        expected = alerts.definitions("channel", ["maintenance"])[0]
        actual = copy.deepcopy(expected)
        actual["conditions"][0]["name"] = "projects/example/conditions/123"
        actual["alertStrategy"]["notificationPrompts"] = ["OPENED"]
        self.assertEqual(alerts.comparable(actual), alerts.comparable(expected))
        actual["enabled"] = False
        self.assertNotEqual(alerts.comparable(actual), alerts.comparable(expected))
        actual["enabled"] = True
        actual["notificationChannels"] = ["other"]
        self.assertNotEqual(alerts.comparable(actual), alerts.comparable(expected))

    def test_service_accounts_are_not_email_recipients(self):
        for address in ("(unset)", "service@project.iam.gserviceaccount.com", "not an email"):
            with self.assertRaisesRegex(RuntimeError, "HUMAN_EMAIL_REQUIRED"):
                alerts.email_address(address)
        self.assertEqual(alerts.email_address(" owner@example.com "), "owner@example.com")


if __name__ == "__main__":
    unittest.main()
