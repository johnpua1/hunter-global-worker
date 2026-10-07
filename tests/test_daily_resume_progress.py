import copy
import datetime as dt
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hunter-global'))
import runner
import foundation
import derived
from test_foundation import MemoryDrive, row


class DailyResumeProgressTests(unittest.TestCase):
    def test_reused_daily_produces_identical_rank_without_remote_daily_reads(self):
        drive = MemoryDrive()
        security = {'market': 'US', 'security_id': 'US-000001', 'ticker': 'AAPL'}
        calendar = [(dt.date(2026, 10, 2) - dt.timedelta(days=i)).isoformat()
                    for i in reversed(range(220))]
        base = [row(day, 100 + i) for i, day in enumerate(calendar)]
        daily = [row('2026-10-05', 321), row('2026-10-06', 322)]
        drive.data['US/BASE/batch-0001.ndjson.gz'] = runner.lines_gz(base)
        path = 'US/DAILY/2026-10-05/part-0001.ndjson.gz'
        drive.data[path] = runner.lines_gz(daily)
        state = SimpleNamespace(checkpoint={'total_batches': 1}, securities=[security])
        with patch('derived.load_market', return_value=state), \
             patch('derived.current_universe', return_value=[security]), \
             patch('derived.now_myt', return_value='2026-10-07T11:00:00+08:00'), \
             patch('derived.daily_segments', return_value=['2026-10-05']):
            derived.build(drive, 'US', '2026-10-05')
            expected = {p: v for p, v in drive.data.items() if '/DERIVED/' in p}
            reused = {'US-000001': copy.deepcopy(daily)}
            del drive.data[path]  # A hidden second history read must fail parity.
            with patch('derived.daily_segments', side_effect=AssertionError('history reread')):
                derived.build(drive, 'US', '2026-10-05', daily_rows=reused)
            self.assertEqual(expected, {p: v for p, v in drive.data.items() if '/DERIVED/' in p})
            self.assertEqual(reused['US-000001'], daily)

    def test_phase2_reuses_daily_and_preserves_calendar_and_prices(self):
        from phase2_runtime import compose_prices
        drive = MemoryDrive()
        sid = 'US-000001'
        drive.data['US/BASE/batch-0001.ndjson.gz'] = runner.lines_gz([row('2026-10-02', 100)])
        daily = [row('2026-10-05', 101)]
        path = 'US/DAILY/2026-10-05/part-0001.ndjson.gz'
        drive.data[path] = runner.lines_gz(daily)
        state = SimpleNamespace(checkpoint={'total_batches': 1}, calendar=['2026-10-02'])
        with patch('runner.load_market', return_value=state), \
             patch('phase2_runtime.daily_segments', return_value=['2026-10-05','2026-10-06']):
            expected = compose_prices(drive, 'US', {sid})
            del drive.data[path]
            self.assertEqual(compose_prices(drive, 'US', {sid}, daily_rows={sid:daily}), expected)

    def test_metadata_reads_use_read_timeout_and_do_not_log_key(self):
        from unittest.mock import Mock
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key, drive.http = 'url', 'secret-do-not-log', Mock()
        response = drive.http.post.return_value
        response.status_code = 200
        for op, result in [('list', {'ok':True,'files':[]}), ('file', {'ok':True,'file':None})]:
            response.json.return_value = result
            with patch.dict(os.environ, {'HUNTER_BRIDGE_READ_TIMEOUT_SECONDS':'90'}), \
                 self.assertLogs('hunter',level='INFO') as logs:
                drive._call(op,path='US/DAILY',_attempts=1)
            self.assertEqual(drive.http.post.call_args.kwargs['timeout'],90)
            self.assertNotIn(drive.key,' '.join(logs.output))

    def test_read_snapshot_preserves_duplicate_guard_and_reports_blocked_path(self):
        drive = MemoryDrive()
        security = {'market': 'US', 'security_id': 'US-000001'}
        path = 'US/DAILY/2026-10-05/part-0001.ndjson.gz'
        drive.data[path] = runner.lines_gz([row('2026-10-05', 100)])
        snapshot = {}
        with patch('foundation.daily_segments', return_value=['2026-10-05']), \
             self.assertLogs('hunter', level='INFO') as logs:
            last, keys = foundation.read_existing(drive, 'US', [security], '2026-10-02', snapshot)
        self.assertEqual(last['US-000001'], '2026-10-05')
        self.assertEqual(len(snapshot['US-000001']), 1)
        self.assertTrue(any('SYNC_READ_PROGRESS market=US completed=1 total=1' in s for s in logs.output))
        with patch('foundation.daily_segments', return_value=['2026-10-05']), \
             patch.object(drive, 'read', side_effect=TimeoutError), \
             self.assertLogs('hunter', level='INFO') as logs:
            with self.assertRaises(TimeoutError):
                foundation.read_existing(drive, 'US', [security], '2026-10-02', {})
        self.assertTrue(any('SYNC_READ_START market=US path=' + path in s for s in logs.output))
        self.assertFalse(any('SYNC_READ_DONE' in s for s in logs.output))
        drive.data[path] = runner.lines_gz([row('2026-10-05', 100)] * 2)
        with patch('foundation.daily_segments', return_value=['2026-10-05']):
            with self.assertRaisesRegex(RuntimeError, 'DAILY_DUPLICATE_STORED'):
                foundation.read_existing(drive, 'US', [security], '2026-10-02', {})

    def test_failed_append_cannot_enter_reused_snapshot(self):
        drive = MemoryDrive()
        security = {'market': 'US', 'security_id': 'US-000001', 'ticker': 'AAPL'}
        snapshot = {}
        with patch('foundation.fetch_security', return_value=([row('2026-10-05', 100)], [], [], None)), \
             patch('foundation.closed_dates_since', return_value=['2026-10-05']), \
             patch.object(drive, 'append', side_effect=RuntimeError('write failed')):
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                foundation.append_daily_date(drive, 'US', '2026-10-05', [security],
                    {'US-000001': '2026-10-02'}, set(), 1, ['2026-10-02'], snapshot)
        self.assertEqual(snapshot, {})

    def test_phase2_failure_has_no_receipt_retry_commits_then_skips(self):
        drive = MemoryDrive()
        path = 'US/CONTROL/DAILY_CHECKPOINT.json'
        original = runner.compact({'market': 'US', 'last_completed_date': '2026-10-06'})
        drive.data[path] = original
        with patch('phase2_runtime.daily', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                runner.finish_phase2_daily(drive, 'US')
        self.assertEqual(drive.data[path], original)
        with patch('phase2_runtime.daily', return_value={'market': 'US'}) as daily, \
             patch.object(drive, 'put', wraps=drive.put) as put:
            runner.finish_phase2_daily(drive, 'US')
            self.assertEqual(put.call_args.kwargs['expected_sha'], runner.digest(original))
            runner.finish_phase2_daily(drive, 'US')
            daily.assert_called_once()
        self.assertEqual(drive.json(path)['phase2_completed_date'], '2026-10-06')

    def test_no_new_session_still_resumes_phase2(self):
        with patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL': 'test', 'APPS_SCRIPT_SHARED_KEY': 'test',
                                    'HUNTER_ACTIONS_CUTOVER': 'CONFIRMED'}), \
             patch.object(sys, 'argv', ['runner', '--mode', 'auto', '--market', 'US']), \
             patch('runner._enforce_cloud_run_topology'), patch('production_guard.check_at_start'), \
             patch('runner.Drive'), patch('foundation.seed_corporate_actions'), \
             patch('foundation.run_daily', return_value=[]), patch('runner.finish_phase2_daily') as finish:
            runner.main()
            self.assertEqual(finish.call_args.args[1], 'US')


if __name__ == '__main__':
    unittest.main()
