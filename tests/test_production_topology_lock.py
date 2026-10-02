import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "cloudrun" / "production-topology-lock.json").read_text(encoding="utf-8"))


class ProductionTopologyLockTests(unittest.TestCase):
    def test_exact_four_jobs(self):
        jobs = {x["name"]: x for x in LOCK["jobs"]}
        self.assertEqual(set(jobs), {
            "hunter-us-daily",
            "hunter-hk-daily",
            "hunter-maintenance",
            "hunter-monthly-v2",
        })

    def test_expected_trigger_authorities(self):
        jobs = {x["name"]: x for x in LOCK["jobs"]}
        self.assertEqual(jobs["hunter-us-daily"]["trigger"]["authority"], "APPS_SCRIPT")
        self.assertEqual(jobs["hunter-hk-daily"]["trigger"]["authority"], "APPS_SCRIPT")
        self.assertEqual(jobs["hunter-maintenance"]["trigger"]["authority"], "CLOUD_SCHEDULER")
        self.assertEqual(jobs["hunter-monthly-v2"]["trigger"]["authority"], "APPS_SCRIPT")

    def test_deploy_scripts_match_locked_job_entrypoints(self):
        daily = (ROOT / "cloudrun" / "deploy.sh").read_text(encoding="utf-8")
        monthly = (ROOT / "cloudrun" / "deploy-monthly.sh").read_text(encoding="utf-8")
        self.assertIn("deploy_job hunter-us-daily", daily)
        self.assertIn("--args='--mode,auto,--market,US'", daily)
        self.assertIn("deploy_job hunter-hk-daily", daily)
        self.assertIn("--args='--mode,auto,--market,HK'", daily)
        self.assertIn("deploy_job hunter-maintenance", daily)
        self.assertIn("--command=python --args=/app/maintenance.py", daily)
        self.assertIn('JOB="hunter-monthly-v2"', monthly)
        self.assertIn("--args='--mode,monthly'", monthly)

    def test_gateway_has_single_locked_handlers(self):
        source = (ROOT / "bridge" / "Gateway.gs").read_text(encoding="utf-8")
        for fn in ("dailyUS", "dailyHK", "monthlyV2"):
            hits = re.findall(r"function\s+" + re.escape(fn) + r"\s*\(", source)
            self.assertEqual(len(hits), 1, fn)
        self.assertNotRegex(source, r"function\s+quarterlyV2\s*\(")
        self.assertIn("handler === 'monthlyV2' || handler === 'quarterlyV2'", source)

    def test_runner_has_monthly_not_quarterly(self):
        source = (ROOT / "hunter-global" / "runner.py").read_text(encoding="utf-8")
        self.assertIn('"monthly"', source)
        self.assertNotIn('if args.mode == "quarterly":', source)

    def test_schedule_script_does_not_create_monthly_scheduler(self):
        source = (ROOT / "cloudrun" / "schedule.sh").read_text(encoding="utf-8")
        self.assertNotIn("upsert_schedule hunter-monthly-v2", source)


if __name__ == "__main__":
    unittest.main()
