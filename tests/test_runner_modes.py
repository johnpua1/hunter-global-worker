import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "hunter-global" / "runner.py"


class RunnerModeContractTests(unittest.TestCase):
    def test_monthly_is_executable_and_quarterly_is_retired(self):
        source = RUNNER.read_text(encoding="utf-8")
        m = re.search(r'parser\.add_argument\("--mode", choices=\((.*?)\), default="probe"\)', source, re.S)
        self.assertIsNotNone(m)
        choices = set(re.findall(r'"([^"]+)"', m.group(1)))
        self.assertIn("monthly", choices)
        self.assertNotIn("quarterly", choices)
        self.assertIn('if args.mode == "monthly":', source)
        self.assertNotIn('if args.mode == "quarterly":', source)


if __name__ == "__main__":
    unittest.main()
