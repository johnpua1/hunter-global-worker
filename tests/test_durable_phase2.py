import copy
import json
import os
import sys
import threading
import unittest
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch
from types import SimpleNamespace

from test_incremental_inputs import Store, open_cache
import test_patch_snapshot_reuse as patch_tests
from runner import compact, digest, finish_phase2_daily
import execution_budget as budget
import phase2_runtime as phase2
import phase2_resume as journal
import runner


class DurableInputTests(unittest.TestCase):
    def test_inventory_read_avoids_duplicate_file_request_and_verifies_bytes(self):
        drive = runner.Drive.__new__(runner.Drive)
        data = b'{"value":1}'
        response = {'data_base64': base64.b64encode(data).decode(), 'sha256': digest(data)}
        with patch.object(drive, 'file', side_effect=AssertionError('redundant metadata read')), \
             patch.object(drive, '_call', return_value=response) as call:
            self.assertEqual(drive.read_known('US/REPAIR_PATCH/x.json', {'size': len(data)}), data)
            self.assertEqual(call.call_args.args[0], 'read')

    def test_hard_kill_at_581_keeps_575_without_exit_handler_both_markets(self):
        for market in ('US', 'HK'):
            store = Store()
            paths = [market + '/REPAIR_PATCH/%04d.json' % i for i in range(664)]
            store.data.update({p: compact({'value': i}) for i, p in enumerate(paths)})
            cache, drive = open_cache(store, market)
            for p in paths[:581]: drive.read(p)
            self.assertEqual(len(cache.manifest['entries']), 575)
            # Abandon the object: no flush, finally block, or shutdown hook.
            store.reads.clear()
            cache, drive = open_cache(store, market)
            for p in paths: self.assertEqual(drive.read(p), store.data[p])
            self.assertEqual(sum(store.reads[p] for p in paths), 89)
            self.assertTrue(all(store.reads[p] == 0 for p in paths[:575]))

    def test_parallel_readers_cannot_lose_dirty_entries_during_checkpoint(self):
        store = Store()
        paths = ['US/REPAIR_PATCH/%04d.json' % i for i in range(104)]
        store.data.update({p: compact({'x': i}) for i, p in enumerate(paths)})
        cache, drive = open_cache(store)
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(drive.read, paths)), [store.data[p] for p in paths])
        cache.flush()
        store.reads.clear()
        restored, drive = open_cache(store)
        self.assertEqual(len(restored.manifest['entries']), 104)
        with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(drive.read, paths))
        self.assertEqual(sum(store.reads[p] for p in paths), 0)

    def test_checkpoint_failure_stops_read_loop_and_preserves_previous_commit(self):
        store = Store()
        paths = ['US/REPAIR_PATCH/%04d.json' % i for i in range(75)]
        store.data.update({p: compact({'x': i}) for i, p in enumerate(paths)})
        cache, drive = open_cache(store)
        for p in paths[:25]: drive.read(p)
        committed = store.data[cache.pointer]
        put = store.put
        def fail_manifest(path, data, **kwargs):
            if path == cache.pointer: raise RuntimeError('SIMULATED_INTERRUPTION')
            return put(path, data, **kwargs)
        with patch.object(store, 'put', side_effect=fail_manifest):
            with self.assertRaisesRegex(RuntimeError, 'SIMULATED_INTERRUPTION'):
                for p in paths[25:]: drive.read(p)
        self.assertEqual(store.data[cache.pointer], committed)
        store.reads.clear()
        _, drive = open_cache(store)
        for p in paths[:25]: drive.read(p)
        self.assertEqual(sum(store.reads[p] for p in paths[:25]), 0)

    def test_time_threshold_flushes_small_batch(self):
        store = Store(); p = 'US/REPAIR_PATCH/a.json'; store.data[p] = b'{}'
        cache, drive = open_cache(store)
        with patch('incremental_inputs.time.monotonic', return_value=cache.checkpoint_at + 121):
            drive.read(p)
        self.assertIn(p, cache.manifest['entries'])

    def test_interrupted_reaction_loading_resumes_and_matches_full_result(self):
        for market in ('US', 'HK'):
            with self.subTest(market=market), ExitStack() as stack:
                original, sid, date, daily, paths = patch_tests.PatchSnapshotTests().fixture(stack, market)
                store = Store(); store.data = original.data
                history = {'events': [{'security_id': sid, 'report_date': '2026-01-26',
                                      'session': 'BMO', 'reaction_status': 'PARTIAL'}]}
                expected = phase2.calculate_reactions(store, market, copy.deepcopy(history), daily_rows=daily)
                cache, drive = open_cache(store, market)
                read = store.read
                def killed(path):
                    if path == paths[581]: raise KeyboardInterrupt('simulated hard kill')
                    return read(path)
                with patch.object(store, 'read', side_effect=killed):
                    with self.assertRaises(KeyboardInterrupt):
                        phase2.calculate_reactions(drive, market, copy.deepcopy(history), daily_rows=daily)
                store.reads.clear()
                cache, drive = open_cache(store, market)
                actual = phase2.calculate_reactions(drive, market, copy.deepcopy(history), daily_rows=daily)
                self.assertEqual(actual, expected)
                self.assertLess(sum(store.reads[p] for p in paths), 100)


class DurablePublicationTests(unittest.TestCase):
    def test_real_auto_pipeline_then_interrupted_commit_resumes_without_reranking(self):
        for market in ('US', 'HK'):
            with self.subTest(market=market), ExitStack() as stack:
                raw, sid, date, daily, paths = patch_tests.PatchSnapshotTests().fixture(stack, market, count=30)
                store = Store(); store.data = raw.data
                security = {'security_id': sid, 'market': market, 'ticker': 'TEST', 'listing_status': 'ACTIVE'}
                cp = market + '/CONTROL/DAILY_CHECKPOINT.json'
                store.data[cp] = compact({'market': market, 'last_completed_date': '2026-02-03'})
                store.data[market + '/CURRENT_UNIVERSE.json'] = compact({'market': market, 'securities': [security]})
                history = {'market': market, 'events': [{'event_id': sid + ':2026-01-26',
                    'security_id': sid, 'report_date': '2026-01-26', 'session': 'BMO', 'reaction_status': 'PARTIAL'}]}
                for name, data in [('SECTOR_MAP', {'rows':[{'security_id':sid}]}),
                                   ('EARNINGS_CALENDAR', {'market':market,'events':[]}), ('EARNINGS_HISTORY',history)]:
                    store.data[market + '/PHASE2/' + name + '.json'] = compact(data)
                state = SimpleNamespace(checkpoint={'total_batches':1,'verified_batches':{'1':{}},
                                                     'as_of':'2026-01-30'}, calendar=[])
                def read_existing(drive, m, securities, anchor, daily_rows):
                    daily_rows.update(daily)
                    return {sid:date}, set()
                for target, kwargs in [
                    ('runner._enforce_cloud_run_topology', {}), ('production_guard.check_at_start', {}),
                    ('runner.Drive', {'return_value':store}), ('foundation.seed_corporate_actions', {}),
                    ('foundation.load_market', {'return_value':state}),
                    ('foundation.current_universe', {'return_value':[security]}),
                    ('foundation.read_existing', {'side_effect':read_existing}),
                    ('foundation.closed_dates_since', {'side_effect':lambda m, d: [] if d == date else [date]}),
                    ('foundation.append_daily_date', {'return_value':{'status':'COMPLETE','written':1}}),
                    ('foundation.update_new_listing_history', {}),
                    ('phase2_runtime.current_universe', {'return_value':[security]}),
                    ('phase2_runtime.' + ('us_calendar' if market == 'US' else 'hk_calendar'),
                     {'return_value':{'market':market,'events':[]}}),
                    ('phase2_runtime.update_history', {'side_effect':lambda m,o,n,h,d:h}),
                ]: stack.enter_context(patch(target, **kwargs))
                stack.enter_context(patch.dict(os.environ, {'APPS_SCRIPT_WEBAPP_URL':'test',
                    'APPS_SCRIPT_SHARED_KEY':'test','HUNTER_ACTIONS_CUTOVER':'CONFIRMED',
                    'HUNTER_INCREMENTAL_INPUTS':'1','HUNTER_RESUMABLE_RUN':'1'}))
                stack.enter_context(patch.object(sys, 'argv', ['runner','--mode','auto','--market',market]))
                put = store.put
                def die_after_history(path, data, **kwargs):
                    result = put(path, data, **kwargs)
                    if path == market + '/PHASE2/EARNINGS_HISTORY.json': raise KeyboardInterrupt()
                    return result
                with patch.object(store, 'put', side_effect=die_after_history):
                    with self.assertRaises(KeyboardInterrupt): runner.main()
                self.assertNotIn('phase2_completed_date', store.json(cp))
                ranking = {p:v for p,v in store.data.items() if '/DERIVED/' in p}
                store.writes.clear(); store.reads.clear()
                runner.main()
                self.assertEqual(store.json(cp)['phase2_completed_date'], date)
                self.assertEqual(ranking, {p:v for p,v in store.data.items() if '/DERIVED/' in p})
                self.assertFalse(any('/DERIVED/' in p for p in store.writes))
                self.assertEqual(sum(store.reads[p] for p in paths), 0)
                budget._started = None

    def fixture(self, market='US'):
        store = Store(); date = '2026-10-06' if market == 'US' else '2026-10-07'
        cp = market + '/CONTROL/DAILY_CHECKPOINT.json'
        store.data[cp] = compact({'market': market, 'last_completed_date': date})
        store.data[market + '/CURRENT_UNIVERSE.json'] = b'{"securities":[]}'
        store.data[market + '/PHASE2/SECTOR_MAP.json'] = b'{"rows":[]}'
        outputs = []
        for name in ('EARNINGS_HISTORY', 'EARNINGS_CALENDAR'):
            path = market + '/PHASE2/' + name + '.json'
            store.data[path] = compact({'market': market, 'events': ['old']})
            data = compact({'market': market, 'events': ['new']})
            outputs.append((path, data, digest(store.data[path])))
        return store, date, cp, outputs

    def test_death_after_history_write_resumes_calendar_and_completion_without_recompute(self):
        for market in ('US', 'HK'):
            store, date, cp, outputs = self.fixture(market)
            _, drive = open_cache(store, market)
            put = store.put
            def die_after_write(path, data, **kwargs):
                result = put(path, data, **kwargs)
                if path == outputs[0][0]: raise KeyboardInterrupt('lost readback')
                return result
            with patch.object(store, 'put', side_effect=die_after_write), \
                 patch('phase2_runtime.daily', side_effect=lambda *a, **kw:
                       journal.stage_and_publish(drive, market, outputs, {'market': market})):
                with self.assertRaises(KeyboardInterrupt):
                    finish_phase2_daily(drive, market, expected_date=date)
            self.assertNotIn('phase2_completed_date', json.loads(store.data[cp]))
            self.assertEqual(store.data[outputs[0][0]], outputs[0][1])
            store.writes.clear()
            _, drive = open_cache(store, market)
            with patch('phase2_runtime.us_calendar', side_effect=AssertionError('recomputed')), \
                 patch('phase2_runtime.hk_calendar', side_effect=AssertionError('recomputed')), \
                 patch('phase2_runtime.calculate_reactions', side_effect=AssertionError('recomputed')):
                finish_phase2_daily(drive, market, expected_date=date)
            self.assertEqual(json.loads(store.data[cp])['phase2_completed_date'], date)
            self.assertNotIn(outputs[0][0], store.writes)
            self.assertEqual(store.writes, [outputs[1][0], cp])

    def staged(self):
        store, date, cp, outputs = self.fixture()
        _, drive = open_cache(store)
        with patch('phase2_resume.publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                journal.stage_and_publish(drive, 'US', outputs, {'market': 'US'})
        return store, cp, outputs

    def test_corrupt_staging_or_changed_output_cannot_mark_completion(self):
        for change in ('blob', 'output'):
            store, cp, outputs = self.staged()
            if change == 'blob':
                blob = next(p for p in store.data if '/PHASE2_RESUME/blob-' in p)
                store.data[blob] = b'bad'
            else: store.data[outputs[0][0]] = b'{"concurrent":"writer"}'
            store.writes.clear()
            _, drive = open_cache(store)
            with self.assertRaises(RuntimeError): finish_phase2_daily(drive, 'US')
            self.assertNotIn('phase2_completed_date', json.loads(store.data[cp]))
            self.assertEqual(store.writes, [])

    def test_changed_source_or_universe_invalidates_staged_computation(self):
        for path in ('US/REPAIR_PATCH/new.json', 'US/CURRENT_UNIVERSE.json'):
            store, cp, outputs = self.staged()
            store.data[path] = b'{"changed":true}'
            _, drive = open_cache(store)
            self.assertIsNone(journal.resume(drive, 'US'))
            self.assertNotIn('phase2_completed_date', json.loads(store.data[cp]))


class ExecutionBudgetTests(unittest.TestCase):
    def tearDown(self):
        budget._started = None

    def test_soft_stop_leaves_checkpoint_reserve_but_hard_reserve_is_bounded(self):
        with patch.dict(os.environ, {'HUNTER_RESUMABLE_RUN': '1'}), \
             patch('execution_budget.time.monotonic', return_value=100): budget.start()
        with patch('execution_budget.time.monotonic', return_value=6101):
            with self.assertRaisesRegex(budget.WorkBudgetExceeded, 'RESUME_REQUIRED'): budget.timeout(90)
            with budget.saving(): self.assertEqual(budget.timeout(90), 90)
            with self.assertRaises(budget.WorkBudgetExceeded): budget.timeout(90)
        with patch('execution_budget.time.monotonic', return_value=6990), budget.saving():
            self.assertEqual(budget.timeout(90), 10)
        with patch('execution_budget.time.monotonic', return_value=7000), budget.saving():
            with self.assertRaises(budget.WorkBudgetExceeded): budget.timeout(90)
