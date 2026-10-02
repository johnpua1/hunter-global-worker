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

    def test_runtime_guards_bind_job_names_to_entry_modes(self):
        runner = (ROOT / "hunter-global" / "runner.py").read_text(encoding="utf-8")
        maint = (ROOT / "hunter-global" / "maintenance.py").read_text(encoding="utf-8")
        self.assertIn('"hunter-us-daily": ("auto", "US")', runner)
        self.assertIn('"hunter-hk-daily": ("auto", "HK")', runner)
        self.assertIn('"hunter-monthly-v2": ("monthly", None)', runner)
        self.assertIn("TOPOLOGY_RUNTIME_MISMATCH", runner)
        self.assertIn('job != "hunter-maintenance"', maint)
        self.assertIn("TOPOLOGY_RUNTIME_MISMATCH", maint)

    def test_myt_timezone_aliases_are_explicitly_limited(self):
        source = (ROOT / "cloudrun" / "enforce-four-job-topology.sh").read_text(encoding="utf-8")
        self.assertIn('{"Asia/Kuala_Lumpur","Asia/Singapore"}', source)
        self.assertNotIn('startswith("Asia/")', source)

    def test_live_enforcer_updates_all_four_images_only(self):
        source = (ROOT / "cloudrun" / "enforce-four-job-topology.sh").read_text(encoding="utf-8")
        self.assertIn('gcloud builds submit "$ROOT" --tag "$IMAGE"', source)
        for job in ("hunter-us-daily","hunter-hk-daily","hunter-maintenance","hunter-monthly-v2"):
            self.assertIn(job, source)
        self.assertIn('gcloud run jobs update "$job" --image "$IMAGE"', source)
        self.assertIn('"USImage"', source)
        self.assertIn('"MONTHImage"', source)
        self.assertIn("UNEXPECTED_HUNTER_JOB", source)
        self.assertIn('US_SA="hunter-us-daily@${GCP_PROJECT_ID}.iam.gserviceaccount.com"', source)
        self.assertIn('HK_SA="hunter-hk-daily@${GCP_PROJECT_ID}.iam.gserviceaccount.com"', source)
        self.assertIn('MAINT_SA="hunter-maintenance@${GCP_PROJECT_ID}.iam.gserviceaccount.com"', source)
        self.assertIn('MONTH_SA="hunter-monthly@${GCP_PROJECT_ID}.iam.gserviceaccount.com"', source)
        self.assertIn("APPS_SCRIPT_SHARED_KEY_US:latest", source)
        self.assertIn("APPS_SCRIPT_SHARED_KEY_HK:latest", source)
        self.assertIn("APPS_SCRIPT_SHARED_KEY_MAINT:latest", source)
        self.assertIn("APPS_SCRIPT_SHARED_KEY_MONTH:latest", source)

    def test_schedule_script_is_maintenance_only(self):
        source = (ROOT / "cloudrun" / "schedule.sh").read_text(encoding="utf-8")
        self.assertNotIn("upsert_schedule hunter-us-daily", source)
        self.assertNotIn("upsert_schedule hunter-hk-daily", source)
        self.assertNotIn("upsert_schedule hunter-monthly-v2", source)
        self.assertIn("upsert_schedule hunter-maintenance", source)
        self.assertIn("DUAL_TRIGGER_RISK", source)


if __name__ == "__main__":
    unittest.main()
