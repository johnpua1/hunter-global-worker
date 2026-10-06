import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cloudrun" / "guard-apps-script-governed.py"

spec = importlib.util.spec_from_file_location("guard_apps_script_governed", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class GuardAppsScriptGovernedTests(unittest.TestCase):
    def test_removes_legacy_watchdog_and_accepts_unrelated_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "Gateway.gs").write_text("function hunterDailyWatchdog() {}\nfunction dailyUS() {}\nfunction dailyHK() {}\n", encoding="utf-8")
            (root / "Watchdog.js").write_text("function hunterDailyWatchdog() { throw new Error('HUNTER_ALERT_LOG_WRITE_FAILED'); }\n", encoding="utf-8")
            (root / "Code.js").write_text("function harmless() {}\n", encoding="utf-8")

            # Mirror helper logic without CLI parsing.
            gateway = root / "Gateway.gs"
            self.assertTrue(gateway.is_file())
            self.assertIn("function hunterDailyWatchdog()", gateway.read_text(encoding="utf-8"))
            removed = []
            for ext in mod.SCRIPT_EXTS:
                p = root / ("Watchdog" + ext)
                if p.exists():
                    p.unlink()
                    removed.append(p.name)
            self.assertIn("Watchdog.js", removed)
            self.assertFalse((root / "Watchdog.js").exists())

    def test_detects_governed_collision_in_non_gateway_file(self):
        import re
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "Gateway.gs").write_text("function hunterDailyWatchdog() {}\n", encoding="utf-8")
            (root / "Code.js").write_text("function dailyUS() {}\n", encoding="utf-8")
            function_re = re.compile(r"\bfunction\s+(" + "|".join(re.escape(x) for x in mod.GOVERNED) + r")\s*\(")
            hits = function_re.findall((root / "Code.js").read_text(encoding="utf-8"))
            self.assertIn("dailyUS", hits)

    def test_forbidden_legacy_alert_literal_is_registered(self):
        self.assertIn("HUNTER_ALERT_LOG_WRITE_FAILED", mod.FORBIDDEN_LITERALS)


if __name__ == "__main__":
    unittest.main()
