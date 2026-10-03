"""Worker exit status must reflect actual DAILY completion."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
import runner


class DailyCompletionTests(unittest.TestCase):
    def invoke(self, market, mode, result):
        phase2 = Mock()
        with patch.dict(os.environ, {
            "APPS_SCRIPT_WEBAPP_URL": "test", "APPS_SCRIPT_SHARED_KEY": "test",
            "HUNTER_ACTIONS_CUTOVER": "CONFIRMED",
        }, clear=True), patch.object(sys, "argv", [
            "runner.py", "--mode", mode, "--market", market,
        ]), patch("runner.Drive"), patch("production_guard.check_at_start"), \
                patch("foundation.seed_corporate_actions"), \
                patch("foundation.run_daily", return_value=result), \
                patch.dict(sys.modules, {"phase2_runtime": SimpleNamespace(daily=phase2)}):
            try:
                runner.main()
            finally:
                if not result or any(x.get("status") != "COMPLETE" for x in result):
                    phase2.assert_not_called()
        return phase2

    def test_outage_fails_all_daily_entrypoints(self):
        for market in ("US", "HK"):
            for mode in ("auto", "daily", "daily-core"):
                with self.subTest(market=market, mode=mode):
                    with self.assertRaisesRegex(RuntimeError, "DAILY_INCOMPLETE"):
                        self.invoke(market, mode, [{
                            "trade_date": "2026-10-02",
                            "status": "MARKET_WIDE_DATA_UNAVAILABLE",
                            "active": 5366, "available": 153, "written": 0,
                        }])

    def test_completed_and_no_session_remain_successful(self):
        for mode in ("auto", "daily", "daily-core"):
            with self.subTest(mode=mode):
                self.invoke("US", mode, [])
                phase2 = self.invoke("US", mode, [{"status": "COMPLETE", "written": 2}])
                self.assertEqual(phase2.call_count, int(mode != "daily-core"))


if __name__ == "__main__":
    unittest.main()
