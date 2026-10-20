"""An unpublished SIC is an honest UNKNOWN, not a failed SEC transport."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
import phase2_runtime as phase2


class SectorMissingSicTests(unittest.TestCase):
    def run_case(self, *, unavailable=False):
        active = {"US-007073": {"ticker": "IVAI"}}
        index = {"IVAI": {"US-007073"}}
        universe = [{"symbol": "OTHER", "sector": "Financial", "industry": "General"}] * 5000
        universe.append({"symbol": "IVAI", "sector": "", "industry": ""})

        def request(url, sec=False):
            if "screener/stocks" in url:
                return Mock(json=lambda: {"data": {"rows": universe}})
            if "company_tickers_exchange" in url:
                return Mock(json=lambda: {"data": [[2077345, "Ives Ultra AI Opportunities", "IVAI", "NYSE"]]})
            if "/submissions/CIK" in url:
                if unavailable:
                    raise RuntimeError("HTTP_503")
                return Mock(json=lambda: {"name": "Ives Ultra AI Opportunities Inc.", "sic": ""})
            raise AssertionError("Unexpected source: " + url)

        with patch.object(phase2, "request", side_effect=request), patch.object(phase2.time, "sleep"):
            rows, errors, ambiguous = phase2.sector_us(active, index, set(active))
        return rows, errors, ambiguous

    def test_verified_no_sic_keeps_unclassified_without_global_failure(self):
        rows, errors, ambiguous = self.run_case()
        self.assertEqual(errors, [])
        self.assertEqual(ambiguous, [])
        self.assertEqual(rows[0]["ticker"], "IVAI")
        self.assertIsNone(rows[0]["source"])
        self.assertEqual(rows[0]["sector"], "UNCLASSIFIED")
        self.assertEqual(rows[0]["reason"], "SEC_SIC_NOT_PUBLISHED")

    def test_actual_sec_transport_failure_remains_error(self):
        rows, errors, _ = self.run_case(unavailable=True)
        self.assertEqual(len(errors), 1)
        self.assertEqual(rows[0]["reason"], "SOURCE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
