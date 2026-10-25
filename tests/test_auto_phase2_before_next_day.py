"""Regression: a completed US RANK with pending Phase2 must not replay foundation."""
import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"hunter-global"))
import runner
from test_foundation import MemoryDrive


class AutoPhase2FirstTests(unittest.TestCase):
    def test_phase2_resumes_without_probing_missing_next_daily(self):
        drive=MemoryDrive()
        date="2026-10-22"
        cp="US/CONTROL/DAILY_CHECKPOINT.json"
        drive.data[cp]=runner.compact({
            "market":"US","last_completed_date":date,
            "phase2_completed_date":"2026-10-21",
            "phase2_completed_at_myt":"2026-10-22T10:58:43+08:00"})
        drive.data[f"US/DERIVED/{date}/RANK.json"]=runner.compact({
            "market":"US","as_of":date,"detail_parts":1,
            "rows":[{"security_id":"US-000001"}]})
        drive.data[f"US/DERIVED/{date}/batch-0001.json"]=b'{}'

        def finish(inputs, market, expected_date=None):
            self.assertEqual(market,"US")
            self.assertEqual(expected_date,date)
            self.assertNotIn("US/CONTROL/DAILY_RUN_2026-10-23.json",drive.data)
            doc=drive.json(cp)
            doc["phase2_completed_date"]=date
            doc["phase2_completed_at_myt"]="2026-10-25T12:00:00+08:00"
            drive.put(cp, runner.compact(doc))
        with patch("runner.closed_dates_since",side_effect=AssertionError("UNNECESSARY_NEXT_DAY_QUERY")) as next_day,\
             patch("runner.validate_phase2_resume") as validate,\
             patch("runner.finish_phase2_daily",side_effect=finish) as phase2,\
             patch("incremental_inputs.incremental_inputs",return_value=contextlib.nullcontext(drive)):
            self.assertTrue(runner.recover_auto_committed(drive,"US"))
        next_day.assert_not_called()
        validate.assert_called_once_with(drive,"US",date)
        phase2.assert_called_once()
        self.assertEqual(drive.json(cp)["phase2_completed_date"],date)

    def test_missing_rank_does_not_fabricate_phase2_completion(self):
        drive=MemoryDrive()
        cp="US/CONTROL/DAILY_CHECKPOINT.json"
        original=runner.compact({"market":"US","last_completed_date":"2026-10-22",
                                 "phase2_completed_date":"2026-10-21"})
        drive.data[cp]=original
        with patch("runner.closed_dates_since") as next_day,\
             patch("runner.finish_phase2_daily") as phase2:
            self.assertFalse(runner.recover_auto_committed(drive,"US"))
        next_day.assert_not_called()
        phase2.assert_not_called()
        self.assertEqual(drive.data[cp],original)


if __name__=="__main__":
    unittest.main()
