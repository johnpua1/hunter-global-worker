"""A killed ranking calculation must retain completed, input-bound batches."""
import copy
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from test_incremental_inputs import Store, fingerprint
from test_patch_snapshot_reuse import PatchSnapshotTests
from runner import compact, lines_gz, parse_lines_gz
from execution_budget import WorkBudgetExceeded
import derived


class MetadataStore(Store):
    def file(self, path):
        return fingerprint(self.data[path]) if path in self.data else None


class DerivedResumeTests(unittest.TestCase):
    def fixture(self, stack, market='US'):
        raw, sid, date, daily, _ = PatchSnapshotTests().fixture(stack, market, count=2)
        store = MetadataStore()
        store.data = copy.deepcopy(raw.data)
        first = parse_lines_gz(store.data[f'{market}/BASE/batch-0001.ndjson.gz'])
        securities = [{'security_id': sid}] + [
            {'security_id': f'{market}-inactive-{i}', 'listing_status': 'INACTIVE'} for i in range(99)]
        for batch in (2, 3):
            other = f'{market}-00000{batch}'
            securities += [{'security_id': other}] + [
                {'security_id': f'{market}-inactive-{batch}-{i}', 'listing_status': 'INACTIVE'} for i in range(99)]
            store.data[f'{market}/BASE/batch-{batch:04d}.ndjson.gz'] = lines_gz([
                {**r, 'security_id': other} for r in first])
            daily[other] = [{**r, 'security_id': other} for r in daily[sid]]
        state = SimpleNamespace(checkpoint={'total_batches': 3}, securities=securities)
        stack.enter_context(patch('derived.load_market', return_value=state))
        stack.enter_context(patch('derived.current_universe', return_value=securities))
        # A deterministic interruption after one committed batch, as at budget expiry.
        stack.enter_context(patch('derived.map_drive_reads', side_effect=
            lambda d, op, items, workers: (op(d, item) for item in items)))
        return store, date, daily

    def test_interrupted_resume_skips_saved_base_and_matches_cold_bytes_both_markets(self):
        for market in ('US', 'HK'):
            with self.subTest(market=market), ExitStack() as stack:
                drive, date, daily = self.fixture(stack, market)
                cold = MetadataStore(); cold.data = copy.deepcopy(drive.data)
                derived.build(cold, market, date, daily_rows=daily)
                read = drive.read
                def stop(path):
                    if path == f'{market}/BASE/batch-0002.ndjson.gz':
                        raise WorkBudgetExceeded('WORK_BUDGET_EXHAUSTED_RESUME_REQUIRED')
                    return read(path)
                snapshot = {'old': True}
                with patch.object(drive, 'read', side_effect=stop):
                    with self.assertRaises(WorkBudgetExceeded):
                        derived.build(drive, market, date, daily_rows=daily, patch_snapshot=snapshot)
                self.assertEqual(snapshot, {})
                self.assertFalse(any('/DERIVED/' in p and '/CONTROL/' not in p for p in drive.data))
                self.assertEqual(len([p for p in drive.data if '/PHASE2_RESUME/' in p]), 1)
                drive.reads.clear()
                with self.assertLogs('hunter', level='INFO') as logs:
                    derived.build(drive, market, date, daily_rows=daily, patch_snapshot=snapshot)
                self.assertTrue(any('DERIVED_BATCH_REUSED' in s for s in logs.output))
                self.assertEqual(drive.reads[f'{market}/BASE/batch-0001.ndjson.gz'], 0)
                self.assertEqual(drive.reads[f'{market}/BASE/batch-0002.ndjson.gz'], 1)
                outputs = lambda d: {p: v for p, v in d.data.items() if p.startswith(f'{market}/DERIVED/')}
                self.assertEqual(outputs(drive), outputs(cold))

    def test_source_and_daily_changes_invalidate_saved_calculation(self):
        with ExitStack() as stack:
            drive, date, daily = self.fixture(stack)
            derived.build(drive, 'US', date, daily_rows=daily)
            path = 'US/BASE/batch-0001.ndjson.gz'
            rows = parse_lines_gz(drive.data[path]); rows[0]['close'] += 10
            drive.data[path] = lines_gz(rows)
            drive.reads.clear()
            derived.build(drive, 'US', date, daily_rows=daily)
            self.assertEqual(drive.reads[path], 1)
            self.assertEqual(drive.reads['US/BASE/batch-0002.ndjson.gz'], 0)
            daily['US-000001'][-1]['close'] += 2
            drive.reads.clear()
            derived.build(drive, 'US', date, daily_rows=daily)
            self.assertEqual(drive.reads['US/BASE/batch-0002.ndjson.gz'], 1)

    def test_corrupt_saved_result_never_publishes_output_or_completion(self):
        with ExitStack() as stack:
            drive, date, daily = self.fixture(stack)
            derived.build(drive, 'US', date, daily_rows=daily)
            path = next(p for p in drive.data if '/PHASE2_RESUME/' in p)
            doc = json.loads(drive.data[path]); doc['rows'][0]['close'] = -123
            drive.data[path] = compact(doc)
            drive.writes.clear()
            with self.assertRaisesRegex(RuntimeError, 'DERIVED_RESUME_RESULT_INVALID'):
                derived.build(drive, 'US', date, daily_rows=daily)
            self.assertEqual(drive.writes, [])

    def test_failed_save_keeps_earlier_results_without_publishing_rank(self):
        with ExitStack() as stack:
            drive, date, daily = self.fixture(stack)
            put = drive.put
            def fail(path, data, **kwargs):
                if '/batch-0002/' in path:
                    raise RuntimeError('write failed')
                return put(path, data, **kwargs)
            with patch.object(drive, 'put', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, 'write failed'):
                    derived.build(drive, 'US', date, daily_rows=daily)
            self.assertEqual(len([p for p in drive.data if '/PHASE2_RESUME/' in p]), 1)
            self.assertFalse(any(p.endswith('/RANK.json') for p in drive.data))


if __name__ == '__main__':
    unittest.main()
