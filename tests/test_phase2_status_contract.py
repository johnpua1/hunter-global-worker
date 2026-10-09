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


if __name__ == "__main__":
    unittest.main()
