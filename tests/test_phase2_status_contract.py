import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("phase2_status", ROOT / "hunter-global" / "phase2_status.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class StatusTests(unittest.TestCase):
    def test_no_phase2_receipt_does_not_revoke_baseline(self):
        sample = {"market": "US", "last_completed_date": "2026-10-08"}
        out = mod.interpret_checkpoint("US", sample)
        self.assertEqual(out["phase2_daily_freshness"], "NOT_RECORDED")

    def test_old_phase2_date_is_behind_not_failed(self):
        sample = {"market": "HK", "last_completed_date": "2026-10-08",
                  "phase2_completed_date": "2026-10-06"}
        out = mod.interpret_checkpoint("HK", sample)
        self.assertEqual(out["phase2_daily_freshness"], "BEHIND")

    def test_same_date_is_current(self):
        sample = {"market": "HK", "last_completed_date": "2026-10-08",
                  "phase2_completed_date": "2026-10-08"}
        out = mod.interpret_checkpoint("HK", sample)
        self.assertEqual(out["phase2_daily_freshness"], "CURRENT")

    def test_live_projection_does_not_access_history(self):
        checkpoints = {
            "US/CONTROL/DAILY_CHECKPOINT.json":
                {"market": "US", "last_completed_date": "2026-10-08"},
            "HK/CONTROL/DAILY_CHECKPOINT.json":
                {"market": "HK", "last_completed_date": "2026-10-08",
                 "phase2_completed_date": "2026-10-06"},
        }
        class ReadOnly:
            def __init__(self):
                self.paths = []
            def json(self, path):
                self.paths.append(path)
                return checkpoints[path]
        read_only = ReadOnly()
        state = mod.project(read_only)
        self.assertEqual(state["historical_acceptance"]["status"], "PASS")
        self.assertEqual(state["production_activation"]["status"], "ACTIVE")
        self.assertEqual(read_only.paths, list(checkpoints))

    def test_registry_has_no_unscoped_pass(self):
        import json
        record = json.loads((ROOT / "docs/hunter-phase2-production-pass-20261009.json")
                            .read_text(encoding="utf-8"))
        self.assertNotIn("acceptance", record)
        self.assertNotIn("status", record)
        self.assertEqual(record["historical_acceptance"]["status"], "PASS")
        self.assertEqual(record["phase2_daily_freshness"]["status"],
                         "LIVE_FROM_CHECKPOINT_ONLY")


    def test_runner_does_not_use_all_empty_result_for_phase2(self):
        runner = (ROOT / "hunter-global" / "runner.py").read_text(encoding="utf-8")
        self.assertIn('if not result:', runner)
        self.assertIn('PHASE2_IDLE_NO_NEW_SESSION', runner)
        self.assertNotIn('if all(item.get("status") == "COMPLETE" for item in result):\n                finish_phase2_daily', runner)


if __name__ == "__main__":
    unittest.main()
