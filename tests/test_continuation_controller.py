import ast
import contextlib
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from test_foundation import MemoryDrive
from runner import compact

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHA = 'a' * 40


class ContinuationControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        mode = patch.dict(os.environ, {'HUNTER_CONTINUE_ONLY':'1'})
        mode.start(); self.addCleanup(mode.stop)
        source = (ROOT / 'cloudrun/phase2-continuation-20261008.sh').read_text().split("<<'PHASE2_PY'",1)[1].split('\n',1)[1].rsplit('\nPHASE2_PY',1)[0]
        tree = ast.parse(source); tree.body.pop()
        self.mod = types.ModuleType('continuation_controller')
        with patch.object(sys,'argv',['controller',str(ROOT),SHA]), patch.object(pathlib.Path,'home',return_value=pathlib.Path(self.temp.name)), patch('fcntl.flock'):
            exec(compile(tree,'controller','exec'),self.mod.__dict__)
        self.addCleanup(self.mod.LOCK.close)

    def test_both_markets_pin_same_new_sha_with_distinct_closed_dates(self):
        self.assertEqual(self.mod.EXPECTED_SHA, {'US':SHA,'HK':SHA})
        self.assertEqual(self.mod.GOAL, {'US':'2026-10-07','HK':'2026-10-08'})

    def test_final_acceptance_reads_only_existing_stage_control_records(self):
        m=self.mod; drive=MemoryDrive(); date='2026-10-08'
        drive.data['HK/CONTROL/DAILY_CHECKPOINT.json']=compact({'market':'HK','last_completed_date':date,'phase2_completed_date':date,'phase2_completed_at_myt':'2026-10-08T21:00:00+08:00'})
        drive.data['HK/CONTROL/DAILY_RUN_'+date+'.json']=compact({'market':'HK','trade_date':date,'status':'COMPLETE'})
        with patch.object(m,'reader',return_value=contextlib.nullcontext(drive)):
            m.full_proof({},'HK',date)
        self.assertEqual(drive.writes,[])

    def test_missing_completion_cannot_be_accepted(self):
        m=self.mod; drive=MemoryDrive(); date='2026-10-08'
        drive.data['HK/CONTROL/DAILY_CHECKPOINT.json']=compact({'market':'HK','last_completed_date':date})
        drive.data['HK/CONTROL/DAILY_RUN_'+date+'.json']=compact({'market':'HK','trade_date':date,'status':'COMPLETE'})
        with patch.object(m,'reader',return_value=contextlib.nullcontext(drive)):
            with self.assertRaisesRegex(RuntimeError,'FINAL_DAILY_RANK_PHASE2_MISMATCH'): m.full_proof({},'HK',date)

    def test_saved_daily_record_avoids_a_redundant_source_gate(self):
        m=self.mod; drive=MemoryDrive()
        drive.data['US/CONTROL/DAILY_RUN_2026-10-07.json']=compact({'market':'US','trade_date':'2026-10-07','status':'COMPLETE'})
        with patch.object(m,'reader',return_value=contextlib.nullcontext(drive)):
            self.assertTrue(m.daily_already_saved({},'US'))

    def test_wrong_date_record_cannot_skip_source_gate(self):
        m=self.mod; drive=MemoryDrive()
        drive.data['US/CONTROL/DAILY_RUN_2026-10-07.json']=compact({'market':'US','trade_date':'2026-10-06','status':'COMPLETE'})
        with patch.object(m,'reader',return_value=contextlib.nullcontext(drive)):
            self.assertFalse(m.daily_already_saved({},'US'))

    def test_new_saved_batches_reset_stalled_retry_counter_only_once(self):
        m=self.mod; key=('US','NEW_SESSION','2026-10-06'); attempts={key:3}
        with patch.object(m.r,'gc',return_value=[{'textPayload':'DERIVED_BATCH_SAVED'}]) as gc, patch.object(m,'emit'):
            m.credit_saved_progress('US','hunter-us-daily-new',key,attempts)
            self.assertEqual(attempts[key],0)
            attempts[key]=3
            m.credit_saved_progress('US','hunter-us-daily-new',key,attempts)
            self.assertEqual(attempts[key],3)
            self.assertEqual(gc.call_count,1)

    def test_no_saved_progress_keeps_stalled_retry_limit(self):
        m=self.mod; key=('US','NEW_SESSION','2026-10-06'); attempts={key:3}
        with patch.object(m.r,'gc',return_value=[]):
            m.credit_saved_progress('US','hunter-us-daily-new',key,attempts)
        self.assertEqual(attempts[key],3)


if __name__ == '__main__': unittest.main()
