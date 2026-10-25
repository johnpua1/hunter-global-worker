import copy
import contextlib
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

    def test_auto_fast_path_commits_phase2_without_loading_foundation(self):
        drive = MemoryDrive()
        date = '2026-10-15'
        cp = 'US/CONTROL/DAILY_CHECKPOINT.json'
        drive.data[cp] = runner.compact({'market': 'US', 'last_completed_date': date})
        drive.data[f'US/DERIVED/{date}/RANK.json'] = runner.compact({
            'market': 'US', 'as_of': date, 'detail_parts': 1,
            'rows': [{'security_id': 'US-000001'}]})
        drive.data[f'US/DERIVED/{date}/batch-0001.json'] = b'{}'
        with patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL': 'test',
                                     'APPS_SCRIPT_SHARED_KEY': 'test',
                                     'HUNTER_ACTIONS_CUTOVER': 'CONFIRMED',
                                     'HUNTER_INCREMENTAL_INPUTS': '0'}), \
             patch.object(sys, 'argv', ['runner', '--mode', 'auto', '--market', 'US']), \
             patch('runner._enforce_cloud_run_topology'), patch('production_guard.check_at_start'), \
             patch('runner.closed_dates_since', return_value=[]), \
             patch('runner.Drive', return_value=drive), \
             patch('foundation.seed_corporate_actions') as seed, \
             patch('foundation.run_daily') as foundation_daily, \
             patch('phase2_runtime.daily', return_value={'market': 'US'}) as phase2:
            runner.main()
            phase2.assert_called_once()
            foundation_daily.assert_not_called()
            seed.assert_not_called()
        self.assertEqual(drive.json(cp)['phase2_completed_date'], date)
        self.assertTrue(drive.json(cp)['phase2_completed_at_myt'])

    def test_auto_fast_path_done_receipt_exits_without_history_scan(self):
        drive = MemoryDrive()
        date = '2026-10-15'
        drive.data['US/CONTROL/DAILY_CHECKPOINT.json'] = runner.compact({
            'market': 'US', 'last_completed_date': date,
            'phase2_completed_date': date,
            'phase2_completed_at_myt': '2026-10-16T07:00:00+08:00'})
        drive.data[f'US/DERIVED/{date}/RANK.json'] = runner.compact({
            'market': 'US', 'as_of': date, 'detail_parts': 1,
            'rows': [{'security_id': 'US-000001'}]})
        drive.data[f'US/DERIVED/{date}/batch-0001.json'] = b'{}'
        with patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL': 'test',
                                     'APPS_SCRIPT_SHARED_KEY': 'test',
                                     'HUNTER_ACTIONS_CUTOVER': 'CONFIRMED',
                                     'HUNTER_INCREMENTAL_INPUTS': '0'}), \
             patch.object(sys, 'argv', ['runner', '--mode', 'auto', '--market', 'US']), \
             patch('runner._enforce_cloud_run_topology'), patch('production_guard.check_at_start'), \
             patch('runner.closed_dates_since', return_value=[]), \
             patch('runner.Drive', return_value=drive), \
             patch('foundation.seed_corporate_actions') as seed, \
             patch('foundation.run_daily') as foundation_daily, \
             patch('runner.finish_phase2_daily') as phase2:
            runner.main()
            phase2.assert_not_called()
            foundation_daily.assert_not_called()
            seed.assert_not_called()

    def test_auto_fast_path_prioritizes_newer_complete_receipt(self):
        drive = MemoryDrive()
        earlier, date = '2026-10-14', '2026-10-15'
        cp_path = 'US/CONTROL/DAILY_CHECKPOINT.json'
        drive.data[cp_path] = runner.compact({
            'market': 'US', 'last_completed_date': earlier,
            'phase2_completed_date': earlier,
            'phase2_completed_at_myt': '2026-10-15T08:00:00+08:00'})
        drive.data[f'US/CONTROL/DAILY_RUN_{date}.json'] = runner.compact({
            'market': 'US', 'trade_date': date, 'status': 'COMPLETE', 'active': 1, 'available': 1})
        def finish_rank(_drive, _market):
            cp = _drive.json(cp_path)
            cp.update(last_completed_date=date)
            _drive.put(cp_path, runner.compact(cp))
            _drive.data[f'US/DERIVED/{date}/RANK.json'] = runner.compact({
                'market': 'US', 'as_of': date, 'detail_parts': 1,
                'rows': [{'security_id': 'US-000001'}]})
            _drive.data[f'US/DERIVED/{date}/batch-0001.json'] = b'{}'
            return f'CLOSED market=US date={date}'
        with patch('runner.closed_dates_since',
                   side_effect=lambda market, last: [date] if last == earlier else []), \
             patch('daily_rank_autoclose.close_one', side_effect=finish_rank) as rank, \
             patch('phase2_runtime.daily', return_value={'market': 'US'}):
            self.assertTrue(runner.recover_auto_committed(drive, 'US'))
            rank.assert_called_once()
        self.assertEqual(drive.json(cp_path)['last_completed_date'], date)
        self.assertEqual(drive.json(cp_path)['phase2_completed_date'], date)

    def test_auto_missing_shards_closes_receipt_for_both_markets(self):
        from daily_rank_autoclose import NotReady
        for market, earlier, date in (
                ('US', '2026-10-21', '2026-10-22'),
                ('HK', '2026-10-22', '2026-10-23')):
            with self.subTest(market=market):
                drive = MemoryDrive()
                cp_path = f'{market}/CONTROL/DAILY_CHECKPOINT.json'
                drive.data[cp_path] = runner.compact({
                    'market': market, 'last_completed_date': earlier,
                    'phase2_completed_date': earlier,
                    'phase2_completed_at_myt': '2026-10-23T07:00:00+08:00'})
                drive.data[f'{market}/CONTROL/DAILY_RUN_{date}.json'] = runner.compact({
                    'market': market, 'trade_date': date,
                    'status': 'COMPLETE', 'active': 2, 'available': 2})

                class CachedProxy:
                    def __getattr__(self, name):
                        return getattr(drive, name)

                cached = CachedProxy()

                @contextlib.contextmanager
                def cached_inputs(raw, requested_market):
                    self.assertIs(raw, drive)
                    self.assertEqual(requested_market, market)
                    yield cached

                def closeout(_drive, _market, max_sessions=None):
                    self.assertIs(_drive, cached)
                    self.assertEqual(_market, market)
                    self.assertEqual(max_sessions, 1)
                    self.assertEqual(_drive.json(cp_path)['last_completed_date'], earlier)
                    _drive.data[f'{market}/DERIVED/{date}/RANK.json'] = runner.compact({
                        'market': market, 'as_of': date, 'detail_parts': 1,
                        'rows': [{'security_id': f'{market}-000001'},
                                 {'security_id': f'{market}-000002'}]})
                    _drive.data[f'{market}/DERIVED/{date}/batch-0001.json'] = b'{}'
                    updated = dict(_drive.json(cp_path), last_completed_date=date)
                    _drive.put(cp_path, runner.compact(updated))
                    return [date]

                with patch('runner.closed_dates_since',
                           side_effect=lambda m, last: [date] if last == earlier else []), \
                     patch('daily_rank_autoclose.close_one',
                           side_effect=NotReady('CURRENT_DATE_DERIVED_SNAPSHOT_NOT_READY')), \
                     patch('incremental_inputs.incremental_inputs',
                           side_effect=cached_inputs) as incremental, \
                     patch('daily_closeout.closeout_committed_receipts',
                           side_effect=closeout) as rebuild, \
                     patch('phase2_runtime.daily',
                           return_value={'market': market}) as phase2:
                    self.assertTrue(runner.recover_auto_committed(drive, market))
                    self.assertEqual(incremental.call_count, 2)
                    self.assertTrue(all(call.args == (drive, market)
                                        for call in incremental.call_args_list))
                    rebuild.assert_called_once()
                    phase2.assert_called_once()
                final = drive.json(cp_path)
                self.assertEqual(final['last_completed_date'], date)
                self.assertEqual(final['phase2_completed_date'], date)
                self.assertTrue(final['phase2_completed_at_myt'])

    def test_auto_partial_current_shards_do_not_trigger_full_rebuild(self):
        from daily_rank_autoclose import NotReady
        drive = MemoryDrive()
        cp_path = 'US/CONTROL/DAILY_CHECKPOINT.json'
        drive.data[cp_path] = runner.compact({
            'market': 'US', 'last_completed_date': '2026-10-21'})
        drive.data['US/CONTROL/DAILY_RUN_2026-10-22.json'] = runner.compact({
            'market': 'US', 'trade_date': '2026-10-22', 'status': 'COMPLETE',
            'active': 5358, 'available': 5307})
        with patch('runner.closed_dates_since', return_value=['2026-10-22']), \
             patch('daily_rank_autoclose.close_one',
                   side_effect=NotReady('CURRENT_DATE_BATCHES_INCOMPLETE:20/54')), \
             patch('daily_closeout.closeout_committed_receipts') as rebuild:
            self.assertFalse(runner.recover_auto_committed(drive, 'US'))
            rebuild.assert_not_called()
        self.assertEqual(drive.json(cp_path)['last_completed_date'], '2026-10-21')

    def test_no_new_session_still_resumes_phase2(self):
        drive = MemoryDrive()
        date = '2026-10-06'
        drive.data['US/CONTROL/DAILY_CHECKPOINT.json'] = runner.compact({
            'market': 'US', 'last_completed_date': date})
        drive.data[f'US/DERIVED/{date}/RANK.json'] = runner.compact({
            'market': 'US', 'as_of': date, 'detail_parts': 1,
            'rows': [{'security_id': 'US-000001'}]})
        drive.data[f'US/DERIVED/{date}/batch-0001.json'] = b'{}'
        def commit_phase2(inputs, market, expected_date=None):
            self.assertEqual(expected_date, date)
            cp_path = 'US/CONTROL/DAILY_CHECKPOINT.json'
            cp = drive.json(cp_path)
            cp.update(phase2_completed_date=date,
                      phase2_completed_at_myt='2026-10-07T08:00:00+08:00')
            drive.put(cp_path, runner.compact(cp))
        with patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL': 'test', 'APPS_SCRIPT_SHARED_KEY': 'test',
                                    'HUNTER_ACTIONS_CUTOVER': 'CONFIRMED', 'HUNTER_INCREMENTAL_INPUTS': '0'}), \
             patch.object(sys, 'argv', ['runner', '--mode', 'auto', '--market', 'US']), \
             patch('runner._enforce_cloud_run_topology'), patch('production_guard.check_at_start'), \
             patch('runner.Drive', return_value=drive), patch('foundation.seed_corporate_actions'), \
             patch('foundation.run_daily', return_value=[]), \
             patch('runner.finish_phase2_daily', side_effect=commit_phase2) as finish:
            runner.main()
            finish.assert_called_once_with(drive, 'US', expected_date=date)

    def test_no_new_session_does_not_recompute_already_completed_phase2(self):
        drive = MemoryDrive()
        date = '2026-10-06'
        drive.data['US/CONTROL/DAILY_CHECKPOINT.json'] = runner.compact({
            'market': 'US', 'last_completed_date': date,
            'phase2_completed_date': date, 'phase2_completed_at_myt': '2026-10-07T08:00:00+08:00'})
        with patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL': 'test', 'APPS_SCRIPT_SHARED_KEY': 'test',
                                    'HUNTER_ACTIONS_CUTOVER': 'CONFIRMED', 'HUNTER_INCREMENTAL_INPUTS': '0'}), \
             patch.object(sys, 'argv', ['runner', '--mode', 'auto', '--market', 'US']), \
             patch('runner._enforce_cloud_run_topology'), patch('production_guard.check_at_start'), \
             patch('runner.Drive', return_value=drive), patch('foundation.seed_corporate_actions'), \
             patch('foundation.run_daily', return_value=[]), patch('runner.finish_phase2_daily') as finish:
            runner.main()
            finish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
