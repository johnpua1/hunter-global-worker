import copy
import json
import os
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import requests
from test_derived_resume import DerivedResumeTests, MetadataStore
from test_foundation import MemoryDrive
from runner import Drive, compact, digest, validate_phase2_resume
from continuation import OutputProgress
import derived
import phase2_resume


class ContinuationPolicyTests(unittest.TestCase):
    def setUp(self):
        mode = patch.dict(os.environ, {'HUNTER_CONTINUE_ONLY': '1'})
        mode.start(); self.addCleanup(mode.stop)

    def test_put_and_append_confirm_response_without_any_readback_or_retry(self):
        drive = Drive.__new__(Drive)
        for method in ('put', 'append'):
            with self.subTest(method=method), patch.object(drive, 'read', side_effect=AssertionError('readback')), \
                 patch.object(drive, 'file', side_effect=AssertionError('metadata readback')), \
                 patch.object(drive, '_call', return_value={'sha256': digest(b'x'), 'file': {'id': 'saved'}}) as call:
                self.assertEqual(getattr(drive, method)('US/test.json', b'x'), {'id': 'saved'})
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.kwargs['_attempts'], 1)
            with patch.object(drive, '_call', side_effect=requests.Timeout), \
                 patch.object(drive, 'read', side_effect=AssertionError('ambiguous readback')):
                with self.assertRaises(requests.Timeout):
                    getattr(drive, method)('US/test.json', b'x')

    def test_single_execution_reads_needed_input_only_once_and_uses_acknowledged_write(self):
        drive = Drive.__new__(Drive)
        drive._continuation_state = {'guard': threading.Lock(), 'locks': {}, 'memory': {}}
        with patch.object(drive, '_read', return_value=b'input') as raw:
            self.assertEqual(drive.read('US/a'), b'input')
            self.assertEqual(drive.read('US/a'), b'input')
            raw.assert_called_once()
            with patch.object(drive, '_call', return_value={'sha256': digest(b'new'), 'file': {}}):
                drive.put('US/a', b'new')
            self.assertEqual(drive.read('US/a'), b'new')
            raw.assert_called_once()

    def test_ambiguous_write_blocks_followup_writes_and_exit_cache_flush(self):
        from incremental_inputs import incremental_inputs
        from unittest.mock import Mock
        drive = Drive.__new__(Drive)
        drive._continuation_state = {'guard': threading.Lock(), 'locks': {}, 'memory': {}}
        with patch.object(drive, '_call', side_effect=requests.Timeout) as call:
            with self.assertRaises(requests.Timeout): drive.put('US/a', b'new')
            with self.assertRaisesRegex(RuntimeError, 'OUTCOME_UNKNOWN'): drive.put('US/b', b'new')
            self.assertEqual(call.call_count, 1)
        cache = Mock(misses=0, hits=0)
        with patch.dict(os.environ, {'HUNTER_INCREMENTAL_INPUTS': '1'}), \
             patch('incremental_inputs.InputCache', return_value=cache):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                with incremental_inputs(drive, 'US'):
                    raise RuntimeError('stop')
        cache.flush.assert_not_called()

    def test_output_receipt_skips_completed_file_without_reading_or_writing_it(self):
        drive = MemoryDrive(); path = 'US/DERIVED/2026-10-07/RANK.json'
        first = OutputProgress(drive, 'US', '2026-10-07', 'TEST')
        first.put(path, b'{}')
        drive.writes.clear()
        read = drive.read
        with patch.object(drive, 'read', side_effect=lambda p: (_ for _ in ()).throw(AssertionError('completed output read')) if p == path else read(p)):
            restored = OutputProgress(drive, 'US', '2026-10-07', 'TEST')
            restored.put(path, b'{}')
        self.assertEqual(drive.writes, [])

    def test_uncertain_output_cannot_be_replayed_or_counted_complete(self):
        drive = MemoryDrive(); path = 'US/DERIVED/2026-10-07/RANK.json'
        progress = OutputProgress(drive, 'US', '2026-10-07', 'TEST')
        put = drive.put
        with patch.object(drive, 'put', side_effect=lambda p, d, **kw: (_ for _ in ()).throw(requests.Timeout()) if p == path else put(p, d, **kw)):
            with self.assertRaises(requests.Timeout): progress.put(path, b'{}')
        drive.writes.clear()
        restored = OutputProgress(drive, 'US', '2026-10-07', 'TEST')
        with self.assertRaisesRegex(RuntimeError, 'OUTCOME_UNKNOWN'): restored.put(path, b'{}')
        self.assertEqual(drive.writes, [])

    def test_completed_ranking_is_skipped_before_input_reads(self):
        with ExitStack() as stack:
            drive, date, daily = DerivedResumeTests().fixture(stack)
            derived.build(drive, 'US', date, daily_rows=daily)
            drive.reads.clear(); drive.writes.clear()
            with patch('derived.load_market', side_effect=AssertionError('completed stage restart')):
                self.assertEqual(derived.build(drive, 'US', date, daily_rows=daily), 3)
            self.assertFalse(any('/BASE/' in p or p.startswith('US/DERIVED/') for p in drive.reads))
            self.assertEqual(drive.writes, [])

    def test_phase2_resume_uses_committed_checkpoint_without_rank_scan(self):
        drive = MemoryDrive()
        cp = {'market': 'HK', 'last_completed_date': '2026-10-07'}
        drive.data['HK/CONTROL/DAILY_CHECKPOINT.json'] = compact(cp)
        self.assertEqual(validate_phase2_resume(drive, 'HK', '2026-10-07'), cp)
        self.assertEqual(drive.writes, [])

    def test_completed_daily_fetch_is_not_repeated_when_ranking_is_pending(self):
        import foundation
        from types import SimpleNamespace
        drive = MemoryDrive(); date = '2026-10-07'; sid = 'US-000001'
        cp_path = 'US/CONTROL/DAILY_CHECKPOINT.json'
        drive.data[cp_path] = compact({'market': 'US', 'last_completed_date': '2026-10-06'})
        run_path = 'US/CONTROL/DAILY_RUN_' + date + '.json'
        drive.data[run_path] = compact({'market': 'US', 'trade_date': date, 'status': 'COMPLETE', 'written': 4})
        state = SimpleNamespace(checkpoint={'total_batches': 1, 'verified_batches': {'1': {}}, 'as_of': '2026-09-25'}, calendar=[])
        with patch('foundation.load_market', return_value=state), \
             patch('foundation.current_universe', return_value=[{'security_id': sid}]), \
             patch('foundation.read_existing', return_value=({sid: date}, set())), \
             patch('foundation.closed_dates_since', return_value=[date]), \
             patch('foundation.append_daily_date', side_effect=AssertionError('completed fetch repeated')), \
             patch('foundation.update_new_listing_history'), patch('derived.build') as build:
            foundation.run_daily(drive, 'US', 4)
        build.assert_called_once()
        self.assertNotIn(run_path, drive.writes)
        self.assertEqual(drive.json(cp_path)['last_completed_date'], date)

    def test_phase2_publish_does_not_read_its_completed_outputs(self):
        drive = MemoryDrive(); date = '2026-10-07'
        paths = ['US/PHASE2/EARNINGS_HISTORY.json', 'US/PHASE2/EARNINGS_CALENDAR.json']
        data = {p: compact({'market': 'US', 'events': []}) for p in paths}
        doc = {'schema': 1, 'context': {'as_of': date}, 'result': {'market': 'US'},
               'outputs': [{'path': p, 'sha256': digest(data[p]), 'expected_sha256': 'old', 'blobs': []} for p in paths]}
        phase2_resume.publish(drive, 'US', doc, fresh=data)
        drive.writes.clear()
        # No blobs are available. A completed-output read would fail the test.
        self.assertEqual(phase2_resume.publish(drive, 'US', doc), {'market': 'US'})
        self.assertFalse(any(p in drive.writes for p in paths))


if __name__ == '__main__': unittest.main()
