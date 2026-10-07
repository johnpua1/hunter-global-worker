import copy
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hunter-global'))
import runner
import phase2_runtime as phase2
from test_foundation import MemoryDrive, row


DATE = '2026-10-06'
CP = 'US/CONTROL/DAILY_CHECKPOINT.json'
RANK = 'US/DERIVED/' + DATE + '/RANK.json'


def committed_drive():
    drive = MemoryDrive()
    drive.data[CP] = runner.compact({'market': 'US', 'last_completed_date': DATE})
    drive.data[RANK] = runner.compact({'market': 'US', 'as_of': DATE, 'detail_parts': 1,
                                      'rows': [{'security_id': 'US-000001'}]})
    drive.data['US/DERIVED/' + DATE + '/batch-0001.json'] = b'{}'
    return drive


class USPhase2ResumeTests(unittest.TestCase):
    def test_explicit_resume_skips_daily_and_rank_writes(self):
        drive = committed_drive()
        saved = dict(drive.data)
        with patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL': 'test', 'APPS_SCRIPT_SHARED_KEY': 'test',
                                    'HUNTER_ACTIONS_CUTOVER': 'CONFIRMED', 'CLOUD_RUN_JOB': 'hunter-us-daily'}), \
             patch.object(sys, 'argv', ['runner', '--mode', 'auto', '--market', 'US',
                                       '--phase2-only', '--as-of', DATE]), \
             patch('production_guard.check_at_start'), patch('runner.Drive', return_value=drive), \
             patch('foundation.seed_corporate_actions') as seed, \
             patch('foundation.run_daily') as daily, patch('derived.build') as rank, \
             patch('phase2_runtime.daily', return_value={'market': 'US'}):
            runner.main()
        seed.assert_not_called()
        daily.assert_not_called()
        rank.assert_not_called()
        self.assertEqual(drive.writes, [CP])
        self.assertEqual(drive.json(CP)['last_completed_date'], DATE)
        self.assertEqual(drive.json(CP)['phase2_completed_date'], DATE)
        self.assertEqual({k:v for k,v in drive.data.items() if k != CP},
                         {k:v for k,v in saved.items() if k != CP})

    def test_wrong_market_date_or_incomplete_rank_is_rejected_before_phase2(self):
        for market, date in [('XX', DATE), ('US', '2026-10-05'), ('US', None)]:
            drive = committed_drive()
            with self.assertRaises(RuntimeError):
                runner.validate_phase2_resume(drive, market, date)
            self.assertEqual(drive.writes, [])
        drive = committed_drive()
        del drive.data['US/DERIVED/' + DATE + '/batch-0001.json']
        with self.assertRaisesRegex(RuntimeError, 'DETAIL_MISSING'):
            runner.validate_phase2_resume(drive, 'US', DATE)
        with patch('phase2_runtime.daily') as daily:
            with self.assertRaisesRegex(RuntimeError, 'CHECKPOINT_CHANGED'):
                runner.finish_phase2_daily(committed_drive(), 'US', expected_date='2026-10-05')
            daily.assert_not_called()

    def test_override_cannot_be_used_for_wrong_market_job(self):
        for job, market in [('hunter-hk-daily', 'US'), ('hunter-us-daily', 'HK')]:
            with patch.dict(os.environ, {'CLOUD_RUN_JOB': job}), \
                 patch.object(sys, 'argv', ['runner', '--mode', 'auto', '--market', market,
                                           '--phase2-only', '--as-of', DATE]), \
                 patch('runner.Drive') as drive:
                with self.assertRaises(RuntimeError):
                    runner.main()
                drive.assert_not_called()

    def test_us_calendar_source_failure_preserves_calendar_history_and_receipt(self):
        drive = committed_drive()
        sid = 'US-000001'
        for filename, data in [('SECTOR_MAP', {'rows':[{'security_id':sid}]}),
                               ('EARNINGS_CALENDAR', {'market':'US','events':[]}),
                               ('EARNINGS_HISTORY', {'market':'US','events':[]})]:
            drive.data['US/PHASE2/' + filename + '.json'] = runner.compact(data)
        saved = dict(drive.data)
        with patch('phase2_runtime.current_universe', return_value=[
                {'security_id':sid,'ticker':'TEST','listing_status':'ACTIVE'}]), \
             patch('phase2_runtime.us_calendar', return_value={
                 'market':'US','events':[],'source_errors':[{'reason':'HTTP_503'}]}), \
             patch('phase2_runtime.calculate_reactions') as reactions:
            with self.assertRaisesRegex(RuntimeError, 'CALENDAR_SOURCE_INCOMPLETE'):
                runner.finish_phase2_daily(drive, 'US')
            reactions.assert_not_called()
        self.assertEqual(drive.data, saved)
        self.assertEqual(drive.writes, [])

    def test_interrupted_calendar_commit_can_resume_without_replaying_daily(self):
        drive = committed_drive()
        sid = 'US-000001'
        for filename, data in [('SECTOR_MAP', {'rows':[{'security_id':sid}]}),
                               ('EARNINGS_CALENDAR', {'market':'US','events':[]}),
                               ('EARNINGS_HISTORY', {'market':'US','events':[]})]:
            drive.data['US/PHASE2/' + filename + '.json'] = runner.compact(data)
        original_cp = drive.data[CP]
        original_rank = drive.data[RANK]
        actual_put = drive.put
        failures = [True]
        def put(path,data,**kwargs):
            if path.endswith('EARNINGS_CALENDAR.json') and failures:
                failures.pop()
                raise TimeoutError('interrupted calendar commit')
            actual_put(path,data,**kwargs)
        with patch('phase2_runtime.current_universe', return_value=[
                {'security_id':sid,'ticker':'TEST','listing_status':'ACTIVE'}]), \
             patch('phase2_runtime.us_calendar', return_value={
                 'market':'US','events':[],'source_errors':[]}), \
             patch.object(drive,'put',side_effect=put):
            with self.assertRaises(TimeoutError):
                runner.finish_phase2_daily(drive,'US',expected_date=DATE)
            self.assertEqual(drive.data[CP],original_cp)
            runner.finish_phase2_daily(drive,'US',expected_date=DATE)
            self.assertEqual(drive.json(CP)['phase2_completed_date'],DATE)
        self.assertEqual(drive.data[RANK],original_rank)
        self.assertTrue(all('/PHASE2/' in p or p == CP for p in drive.writes))

    def test_bounded_us_reads_match_serial_prices_with_patch_and_empty_session(self):
        drive = MemoryDrive()
        sid = 'US-000001'
        base = [row('2026-10-02',100,sid), row('2026-10-05',102,sid)]
        patch_row = row('2026-10-05',104,sid)
        daily = [row(DATE,106,sid)]
        drive.data['US/BASE/batch-0001.ndjson.gz'] = runner.lines_gz(base)
        drive.data['US/BASE/batch-0002.ndjson.gz'] = runner.lines_gz([row(DATE,80,'US-000002')])
        drive.data['US/REPAIR_PATCH/p.json'] = runner.compact({'items':[patch_row]})
        drive.data['US/DAILY/' + DATE + '/part-0001.ndjson.gz'] = runner.lines_gz(daily)
        state = SimpleNamespace(checkpoint={'total_batches':2},calendar=['2026-10-02','2026-10-05'])
        with patch('runner.load_market',return_value=state), \
             patch('phase2_runtime.daily_segments',return_value=[DATE,'2026-10-07']):
            expected = phase2.compose_prices(drive,'US',{sid})
            lock = threading.Lock()
            counts = {'active':0,'max':0}
            class Reader:
                http = Mock()
                def read(self,path):
                    with lock:
                        counts['active'] += 1
                        counts['max'] = max(counts['max'],counts['active'])
                    try:
                        time.sleep(.01)
                        return drive.data[path]
                    finally:
                        with lock: counts['active'] -= 1
                def json(self,path): return json.loads(self.read(path))
                def list(self,path): return drive.list(path)
            drive.fork_reader = Reader
            with patch.dict(os.environ,{'DERIVED_READ_WORKERS':'2'}):
                actual = phase2.compose_prices(drive,'US',{sid})
            self.assertEqual(actual, expected)
            self.assertEqual(counts['max'],2)
            self.assertIn('2026-10-07',actual[1])
            self.assertEqual(drive.writes,[])


if __name__ == '__main__':
    unittest.main()
