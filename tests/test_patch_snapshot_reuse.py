"""Ranking -> Phase 2 must read each repair file only once per daily run."""
import copy
import datetime as dt
import os
import sys
import unittest
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hunter-global'))
import derived
import foundation
import phase2_runtime as phase2
import runner
from test_foundation import MemoryDrive, row


class CountingDrive(MemoryDrive):
    def __init__(self):
        super().__init__()
        self.reads = Counter()
        self.lists = Counter()

    def read(self, path):
        self.reads[path] += 1
        return super().read(path)

    def list(self, path):
        self.lists[path] += 1
        return super().list(path)


class PatchSnapshotTests(unittest.TestCase):
    def fixture(self, stack, market, count=664):
        drive = CountingDrive()
        sid = market + '-000001'
        days = [(dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(35)]
        security = {'security_id': sid, 'listing_status': 'ACTIVE'}
        state = SimpleNamespace(checkpoint={'total_batches': 1}, securities=[security], calendar=days)
        drive.data[market + '/BASE/batch-0001.ndjson.gz'] = runner.lines_gz(
            [row(day, 100 + i, sid) for i, day in enumerate(days[:30])])
        daily = {sid: [row(day, 130 + i, sid) for i, day in enumerate(days[30:])]}
        paths = []
        for i in range(count):
            path = market + '/REPAIR_PATCH/%04d.json' % i
            payload = {'security_id': sid, 'accepted': i % 3 != 0, 'result': 'RESOLVED',
                       'rows': [row(days[25], 115 + i / 1000, sid)]}
            drive.data[path] = runner.compact({'items': [payload]} if i % 2 else payload)
            paths.append(path)
        drive.data[market + '/CORPORATE_ACTIONS/split.json'] = runner.compact([
            {'security_id': sid, 'effective_date': days[32], 'factor': 2}])
        for target in ('derived.load_market', 'runner.load_market'):
            stack.enter_context(patch(target, return_value=state))
        stack.enter_context(patch('derived.current_universe', return_value=[security]))
        stack.enter_context(patch('phase2_runtime.daily_segments', return_value=days[30:]))
        return drive, sid, days[-1], daily, paths

    def test_664_files_read_once_and_reactions_match_uncached_for_both_markets(self):
        for market in ('HK', 'US'):
            with self.subTest(market=market), ExitStack() as stack:
                drive, sid, date, daily, paths = self.fixture(stack, market)
                history = {'events': [{'security_id': sid, 'report_date': '2026-01-26',
                                       'session': 'BMO', 'reaction_status': 'PARTIAL'}]}
                expected = phase2.calculate_reactions(drive, market, copy.deepcopy(history), daily_rows=daily)
                drive.reads.clear(); drive.lists.clear()
                snapshot = {}
                derived.build(drive, market, date, daily_rows=daily, patch_snapshot=snapshot)
                ranking = {k: v for k, v in drive.data.items() if '/DERIVED/' in k}
                saved = copy.deepcopy(snapshot)
                with self.assertLogs('phase2_runtime', level='INFO') as logs:
                    actual = phase2.calculate_reactions(drive, market, copy.deepcopy(history),
                                                        daily_rows=daily, patch_snapshot=snapshot)
                self.assertEqual(actual, expected)
                self.assertEqual(snapshot, saved)
                self.assertEqual(snapshot['files'], 664)
                self.assertEqual(drive.lists[market + '/REPAIR_PATCH'], 1)
                self.assertTrue(all(drive.reads[p] == 1 for p in paths))
                self.assertTrue(any('PHASE2_PATCH_REUSED' in line for line in logs.output))
                self.assertFalse(any('PHASE2_PATCH_PROGRESS' in line for line in logs.output))
                derived.build(drive, market, date, daily_rows=daily)
                self.assertEqual(ranking, {k: v for k, v in drive.data.items() if '/DERIVED/' in k})

    def test_empty_inventory_is_reused_and_next_build_reads_fresh_files(self):
        with ExitStack() as stack:
            drive, sid, date, daily, _ = self.fixture(stack, 'HK', count=0)
            snapshot = {}
            derived.build(drive, 'HK', date, daily_rows=daily, patch_snapshot=snapshot)
            phase2.compose_prices(drive, 'HK', {sid}, daily_rows=daily, patch_snapshot=snapshot)
            self.assertEqual(drive.lists['HK/REPAIR_PATCH'], 1)
            path = 'HK/REPAIR_PATCH/new.json'
            drive.data[path] = runner.compact({'security_id': sid, 'accepted': True,
                'result': 'RESOLVED', 'rows': [row('2026-01-26', 500, sid)]})
            derived.build(drive, 'HK', date, daily_rows=daily, patch_snapshot=snapshot)
            self.assertEqual(snapshot['files'], 1)
            self.assertEqual(drive.reads[path], 1)

    def test_failed_build_clears_previous_snapshot(self):
        with ExitStack() as stack:
            drive, _, date, daily, _ = self.fixture(stack, 'US', count=1)
            snapshot = {'market': 'US', 'as_of': 'OLD'}
            stack.enter_context(patch.object(drive, 'put', side_effect=RuntimeError('write failed')))
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                derived.build(drive, 'US', date, daily_rows=daily, patch_snapshot=snapshot)
            self.assertEqual(snapshot, {})

    def test_wrong_market_or_date_cannot_write_completion(self):
        drive = MemoryDrive()
        cp = 'HK/CONTROL/DAILY_CHECKPOINT.json'
        drive.data[cp] = runner.compact({'market': 'HK', 'last_completed_date': '2026-10-07'})
        for market, date in [('US', '2026-10-07'), ('HK', '2026-10-06')]:
            with patch('phase2_runtime.daily') as daily:
                with self.assertRaisesRegex(RuntimeError, 'SNAPSHOT_IDENTITY_MISMATCH'):
                    runner.finish_phase2_daily(drive, 'HK', patch_snapshot={'market': market, 'as_of': date})
                daily.assert_not_called()
        self.assertEqual(drive.writes, [])

    def test_auto_entry_runs_real_ranking_and_phase2_with_one_patch_read(self):
        with ExitStack() as stack:
            drive, sid, date, daily, paths = self.fixture(stack, 'US', count=2)
            security = {'security_id': sid, 'listing_status': 'ACTIVE', 'ticker': 'TEST'}
            state = SimpleNamespace(checkpoint={'total_batches': 1, 'verified_batches': {'1': {}},
                                                'as_of': '2026-01-30'}, calendar=[])
            cp = 'US/CONTROL/DAILY_CHECKPOINT.json'
            drive.data[cp] = runner.compact({'market': 'US', 'last_completed_date': '2026-02-03'})
            history = {'market': 'US', 'events': [{'event_id': sid + ':2026-01-26',
                'security_id': sid, 'report_date': '2026-01-26', 'session': 'BMO',
                'reaction_status': 'PARTIAL'}]}
            for name, payload in [('SECTOR_MAP', {'rows': [{'security_id': sid}]}),
                                  ('EARNINGS_CALENDAR', {'market': 'US', 'events': []}),
                                  ('EARNINGS_HISTORY', history)]:
                drive.data['US/PHASE2/' + name + '.json'] = runner.compact(payload)
            def read_existing(drive, market, securities, anchor, daily_rows):
                daily_rows.update(daily)
                return {sid: date}, set()
            for target, kwargs in [
                ('runner._enforce_cloud_run_topology', {}), ('production_guard.check_at_start', {}),
                ('runner.Drive', {'return_value': drive}), ('foundation.seed_corporate_actions', {}),
                ('foundation.load_market', {'return_value': state}),
                ('foundation.current_universe', {'return_value': [security]}),
                ('foundation.read_existing', {'side_effect': read_existing}),
                ('foundation.closed_dates_since', {'return_value': [date]}),
                ('foundation.append_daily_date', {'return_value': {'status': 'COMPLETE', 'written': 1}}),
                ('foundation.update_new_listing_history', {}),
                ('phase2_runtime.current_universe', {'return_value': [security]}),
                ('phase2_runtime.us_calendar', {'return_value': {'market': 'US', 'events': []}}),
                ('phase2_runtime.update_history', {'side_effect': lambda m, o, n, h, d: h}),
            ]:
                stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL': 'test',
                                                       'APPS_SCRIPT_SHARED_KEY': 'test',
                                                       'HUNTER_ACTIONS_CUTOVER': 'CONFIRMED'}))
            stack.enter_context(patch.object(sys, 'argv', ['runner', '--mode', 'auto', '--market', 'US']))
            runner.main()
            self.assertTrue(all(drive.reads[p] == 1 for p in paths))
            self.assertEqual(drive.lists['US/REPAIR_PATCH'], 1)
            self.assertEqual(drive.json(cp)['phase2_completed_date'], date)
            self.assertIn('US/DERIVED/' + date + '/RANK.json', drive.data)

    def test_runner_passes_same_snapshot_and_keeps_markets_separate(self):
        snapshots = []
        def foundation_run(drive, market, workers, daily_rows, patch_snapshot):
            self.assertEqual(patch_snapshot, {})
            patch_snapshot.update(market=market, as_of='2026-10-07', files=0, patches={})
            snapshots.append(patch_snapshot)
            return [{'status': 'COMPLETE', 'written': 1}]
        with patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL': 'test', 'APPS_SCRIPT_SHARED_KEY': 'test',
                                    'HUNTER_ACTIONS_CUTOVER': 'CONFIRMED'}), \
             patch.object(sys, 'argv', ['runner', '--mode', 'auto']), \
             patch('runner._enforce_cloud_run_topology'), patch('production_guard.check_at_start'), \
             patch('runner.Drive'), patch('foundation.seed_corporate_actions'), \
             patch('foundation.run_daily', side_effect=foundation_run), \
             patch('runner.finish_phase2_daily') as finish:
            runner.main()
        self.assertEqual(len(snapshots), 2)
        self.assertIsNot(snapshots[0], snapshots[1])
        for index, call in enumerate(finish.call_args_list):
            self.assertIs(call.kwargs['patch_snapshot'], snapshots[index])


if __name__ == '__main__':
    unittest.main()
