import base64
import contextlib
import gzip
import json
import unittest
from unittest.mock import patch

from test_incremental_inputs import Store, open_cache
from runner import compact, digest
from durable_reads import read_large, CHUNK, _put_checkpoint
from execution_budget import WorkBudgetExceeded


class LargeStore(Store):
    def __init__(self, data, market='US'):
        super().__init__()
        self.path = market + '/PHASE2/EARNINGS_HISTORY.json'
        self.data[self.path] = data
        self.revision = 'file-version-1'
        self.calls = []
        self.stop_at = None
        self.corrupt = False

    def file(self, path):
        if path == self.path: return {'size':len(self.data[path]), 'revision':self.revision}
        return super().file(path)

    def _call(self, op, **f):
        assert op == 'read_verified_chunk'
        assert f['path'] == self.path
        self.calls.append((f['offset'], f['length']))
        if f['offset'] == self.stop_at: raise KeyboardInterrupt('hard kill')
        if f['revision'] != self.revision: raise RuntimeError('READ_SOURCE_CHANGED')
        data = self.data[self.path][f['offset']:f['offset']+f['length']]
        compressed = gzip.compress(data)
        return {'size':len(self.data[self.path]), 'offset':f['offset'], 'length':len(data),
                'eof':f['offset']+len(data)==len(self.data[self.path]), 'revision':self.revision,
                'encoding':'gzip', 'sha256':digest(data), 'compressed_sha256':digest(compressed),
                'file_sha256':digest(self.data[self.path]),
                'data_base64':base64.b64encode(b'bad' if self.corrupt else compressed).decode()}

    def new_process(self):
        if hasattr(self, '_read_state'): del self._read_state
        self.calls.clear(); self.stop_at = None


class DurableLargeReadTests(unittest.TestCase):
    def test_real_size_6289429_read_resumes_from_saved_offset_after_hard_kill(self):
        raw = (b'{"events_by_id":{"US-000001":12345}}\n' * 200000)[:6289429]
        self.assertEqual(len(raw), 6289429)
        for market in ('US','HK'):
            store = LargeStore(raw, market); store.stop_at = CHUNK * 30
            with self.assertRaises(KeyboardInterrupt): read_large(store, store.path, store.file(store.path))
            store.new_process()
            self.assertEqual(read_large(store, store.path, store.file(store.path)), raw)
            self.assertEqual(store.calls[0][0], CHUNK * 30)
            self.assertTrue(all(offset >= CHUNK * 30 for offset, _ in store.calls))
            store.calls.clear()
            self.assertEqual(read_large(store, store.path, store.file(store.path)), raw)
            self.assertEqual(store.calls, [])

    def test_same_size_revision_change_restarts_and_never_splices_versions(self):
        store = LargeStore(b'a' * (CHUNK * 3)); store.stop_at = CHUNK
        with self.assertRaises(KeyboardInterrupt): read_large(store, store.path, store.file(store.path))
        store.new_process(); store.revision = 'file-version-2'; store.data[store.path] = b'b' * (CHUNK * 3)
        self.assertEqual(read_large(store, store.path, store.file(store.path)), store.data[store.path])
        self.assertEqual(store.calls[0][0], 0)

    def test_corrupt_response_never_publishes_a_checkpoint(self):
        store = LargeStore(b'a' * (CHUNK * 3)); store.corrupt = True
        with self.assertRaisesRegex(RuntimeError, 'HASH_MISMATCH'):
            read_large(store, store.path, store.file(store.path))
        self.assertEqual(store.writes, [])

    def test_failed_manifest_write_preserves_previous_offset(self):
        store = LargeStore(b'a' * (CHUNK * 3)); store.stop_at = CHUNK
        with self.assertRaises(KeyboardInterrupt): read_large(store, store.path, store.file(store.path))
        pointer = next(p for p in store.data if p.endswith('/MANIFEST.json'))
        original = store.data[pointer]; put = store.put
        store.new_process()
        def fail(path, data, **kwargs):
            if path == pointer: raise RuntimeError('INTERRUPTED_WRITE')
            return put(path, data, **kwargs)
        with patch.object(store,'put',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'INTERRUPTED_WRITE'):
                read_large(store, store.path, store.file(store.path))
        self.assertEqual(store.data[pointer], original)
        store.new_process()
        self.assertEqual(read_large(store,store.path,store.file(store.path)),store.data[store.path])
        self.assertEqual(store.calls[0][0],CHUNK)

    def test_source_changed_during_final_validation_is_not_returned(self):
        store = LargeStore(b'a' * (CHUNK * 3)); info = store.file(store.path)
        with patch.object(store,'file',side_effect=lambda p: {'revision':'changed','size':len(store.data[p])}
                          if p == store.path else Store.file(store,p)):
            with self.assertRaisesRegex(RuntimeError,'FINAL_IDENTITY'):
                read_large(store,store.path,info)

    def test_tail_pack_merges_do_not_redownload_our_own_verified_writes(self):
        store = Store()
        paths = ['HK/REPAIR_PATCH/%04d.json' % i for i in range(664)]
        store.data.update({p:compact({'value':i}) for i,p in enumerate(paths)})
        cache, drive = open_cache(store,'HK')
        for path in paths: drive.read(path)
        cache.flush()
        self.assertEqual(sum(n for p,n in store.reads.items() if p.endswith('.zip')),0)
        self.assertEqual(len(cache.manifest['entries']),664)

    def test_transient_pack_download_failure_does_not_trigger_full_original_reload(self):
        store=Store(); path='HK/REPAIR_PATCH/x.json'; store.data[path]=b'{}'
        cache,drive=open_cache(store,'HK');drive.read(path);cache.flush()
        cache,drive=open_cache(store,'HK');store.reads.clear();read=store.read
        def interrupted(p):
            if p.endswith('.zip'):raise RuntimeError('LARGE_READ_TRANSPORT_RETRY_REQUIRED')
            return read(p)
        with patch.object(store,'read',side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError,'TRANSPORT_RETRY_REQUIRED'):drive.read(path)
        self.assertEqual(store.reads[path],0)
        self.assertNotIn('0',cache.loaded_packs)


class CheckpointWriteRecoveryTests(unittest.TestCase):
    ERROR = 'BRIDGE_Service error: Drive'
    POINTER = 'HK/CONTROL/READ_CACHE/source/MANIFEST.json'

    def setUp(self):
        sleeping = patch('durable_reads.time.sleep')
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def test_precommit_failures_retry_same_bytes_and_original_guards_at_most_three_times(self):
        for mode in ('immutable', 'cas'):
            with self.subTest(mode=mode):
                store = Store(); payload = b'new-checkpoint'
                kwargs = {'immutable': True, 'mime': 'application/octet-stream'}
                if mode == 'cas':
                    store.data[self.POINTER] = b'old-checkpoint'
                    kwargs = {'expected_sha': digest(b'old-checkpoint')}
                put = store.put; calls = []
                def interrupted(path, data, **options):
                    calls.append((path, data, dict(options)))
                    if len(calls) < 3:
                        raise RuntimeError(self.ERROR)
                    return put(path, data, **options)
                with patch.object(store, 'put', side_effect=interrupted):
                    _put_checkpoint(store, self.POINTER, payload, **kwargs)
                self.assertEqual(calls, [(self.POINTER, payload, kwargs)] * 3)
                self.assertEqual(store.data[self.POINTER], payload)

    def test_lost_commit_response_is_confirmed_without_replaying_cas_or_immutable(self):
        for mode in ('immutable', 'cas'):
            with self.subTest(mode=mode):
                store = Store(); payload = b'committed-checkpoint'
                kwargs = {'immutable': True}
                if mode == 'cas':
                    store.data[self.POINTER] = b'old-checkpoint'
                    kwargs = {'expected_sha': digest(b'old-checkpoint')}
                put = store.put
                def lost_response(path, data, **options):
                    put(path, data, **options)
                    raise RuntimeError(self.ERROR)
                with patch.object(store, 'put', side_effect=lost_response) as writing:
                    _put_checkpoint(store, self.POINTER, payload, **kwargs)
                self.assertEqual(writing.call_count, 1)
                self.assertEqual(store.data[self.POINTER], payload)

    def test_final_readback_can_confirm_the_third_write_without_a_fourth_write(self):
        store = Store(); store.data[self.POINTER] = b'old'
        payload = b'new'; put = store.put; calls = []
        def interrupted(path, data, **options):
            calls.append(dict(options))
            if len(calls) == 3:
                put(path, data, **options)
            raise RuntimeError(self.ERROR)
        with patch.object(store, 'put', side_effect=interrupted):
            _put_checkpoint(store, self.POINTER, payload, expected_sha=digest(b'old'))
        self.assertEqual(calls, [{'expected_sha': digest(b'old')}] * 3)
        self.assertEqual(store.data[self.POINTER], payload)

    def test_unknown_committed_state_retries_readback_without_reissuing_cas(self):
        store = Store(); store.data[self.POINTER] = b'old'
        payload = b'new'; put = store.put
        def lost_response(path, data, **options):
            put(path, data, **options)
            raise RuntimeError(self.ERROR)
        with patch.object(store, 'put', side_effect=lost_response) as writing, \
             patch.object(store, 'read', side_effect=[RuntimeError(self.ERROR),
                 RuntimeError(self.ERROR), payload]) as reading:
            _put_checkpoint(store, self.POINTER, payload, expected_sha=digest(b'old'))
        self.assertEqual(writing.call_count, 1)
        self.assertEqual(reading.call_count, 3)

    def test_unreadable_ambiguous_commit_has_bounded_readbacks_and_no_false_success(self):
        store = Store(); store.data[self.POINTER] = b'old'; put = store.put
        def lost_response(path, data, **options):
            put(path, data, **options)
            raise RuntimeError(self.ERROR)
        with patch.object(store, 'put', side_effect=lost_response) as writing, \
             patch.object(store, 'read', side_effect=RuntimeError(self.ERROR)) as reading:
            with self.assertRaisesRegex(RuntimeError, '^LARGE_READ_TRANSPORT_RETRY_REQUIRED$'):
                _put_checkpoint(store, self.POINTER, b'new', expected_sha=digest(b'old'))
        self.assertEqual(writing.call_count, 1)
        self.assertEqual(reading.call_count, 3)
        self.assertEqual(store.data[self.POINTER], b'new')

    def test_changed_or_deleted_cas_target_is_never_overwritten_or_recreated(self):
        for competing in (b'newer-writer', None):
            with self.subTest(competing=competing):
                store = Store(); store.data[self.POINTER] = b'old'
                def competing_write(path, data, **options):
                    if competing is None:
                        del store.data[path]
                    else:
                        store.data[path] = competing
                    raise RuntimeError(self.ERROR)
                with patch.object(store, 'put', side_effect=competing_write) as writing:
                    with self.assertRaisesRegex(RuntimeError, '^BRIDGE_STALE_WRITE$'):
                        _put_checkpoint(store, self.POINTER, b'our-old-result', expected_sha=digest(b'old'))
                self.assertEqual(writing.call_count, 1)
                self.assertEqual(store.data.get(self.POINTER), competing)

    def test_different_immutable_bytes_are_not_overwritten(self):
        store = Store(); payload = b'our-checkpoint'
        part = 'HK/CONTROL/READ_CACHE/source/part-' + digest(payload) + '.gz'
        def competing_write(path, data, **options):
            store.data[path] = b'different-bytes'
            raise RuntimeError(self.ERROR)
        with patch.object(store, 'put', side_effect=competing_write) as writing:
            with self.assertRaisesRegex(RuntimeError, '^BRIDGE_IMMUTABLE_CONFLICT$'):
                _put_checkpoint(store, part, payload, immutable=True)
        self.assertEqual(writing.call_count, 1)
        self.assertEqual(store.data[part], b'different-bytes')

    def test_persistent_service_failure_preserves_old_pointer_and_caps_write_attempts(self):
        store = Store(); store.data[self.POINTER] = b'old'
        with patch.object(store, 'put', side_effect=RuntimeError(self.ERROR)) as writing:
            with self.assertRaisesRegex(RuntimeError, '^LARGE_READ_TRANSPORT_RETRY_REQUIRED$'):
                _put_checkpoint(store, self.POINTER, b'new', expected_sha=digest(b'old'))
        self.assertEqual(writing.call_count, 3)
        self.assertEqual(store.data[self.POINTER], b'old')

    def test_unrelated_errors_and_budget_failures_propagate_without_retry_or_conversion(self):
        errors = [RuntimeError('BRIDGE_STALE_WRITE'), RuntimeError('BRIDGE_IMMUTABLE_CONFLICT'),
                  RuntimeError('BRIDGE_READ_SHA_MISMATCH:checkpoint'),
                  RuntimeError('LARGE_READ_SOURCE_CHANGED'), RuntimeError(self.ERROR + ':other'),
                  RuntimeError('BRIDGE_Lock timeout: another process was holding the lock for too long.'),
                  WorkBudgetExceeded('CHECKPOINT_BUDGET_EXHAUSTED'), ValueError('invalid-response')]
        for stage in ('put', 'file', 'read'):
            for error in errors:
                with self.subTest(stage=stage, error=str(error)), contextlib.ExitStack() as stack:
                    store = Store(); store.data[self.POINTER] = b'old'
                    writing = stack.enter_context(patch.object(store, 'put', side_effect=
                        error if stage == 'put' else RuntimeError(self.ERROR)))
                    if stage != 'put':
                        stack.enter_context(patch.object(store, stage, side_effect=error))
                    with self.assertRaises(type(error)) as caught:
                        _put_checkpoint(store, self.POINTER, b'new', expected_sha=digest(b'old'))
                    self.assertIs(caught.exception, error)
                    self.assertEqual(writing.call_count, 1)
                    self.assertEqual(store.data[self.POINTER], b'old')

    def test_part_and_cas_manifest_service_failures_recover_in_the_same_large_read(self):
        for failing in ('part', 'manifest'):
            with self.subTest(failing=failing):
                payload = b'a' * CHUNK + b'b' * CHUNK + b'tail'
                store = LargeStore(payload, 'HK'); put = store.put; failed = []
                def lost_response(path, data, **options):
                    put(path, data, **options)
                    target = '/part-' in path if failing == 'part' else (
                        path.endswith('/MANIFEST.json') and 'expected_sha' in options)
                    if target and not failed:
                        failed.append(path)
                        raise RuntimeError(self.ERROR)
                with patch.object(store, 'put', side_effect=lost_response):
                    self.assertEqual(read_large(store, store.path, store.file(store.path)), payload)
                self.assertEqual(len(failed), 1)
                self.assertEqual([offset for offset, _ in store.calls], [0, CHUNK, CHUNK * 2])
                pointer = next(p for p in store.data if p.endswith('/MANIFEST.json'))
                self.assertEqual(json.loads(store.data[pointer])['next_offset'], len(payload))

    def test_exhausted_manifest_retry_retains_saved_offset_for_the_next_process(self):
        store = LargeStore(b'a' * CHUNK + b'b' * CHUNK + b'c' * CHUNK, 'HK')
        store.stop_at = CHUNK
        with self.assertRaises(KeyboardInterrupt):
            read_large(store, store.path, store.file(store.path))
        pointer = next(p for p in store.data if p.endswith('/MANIFEST.json'))
        original = store.data[pointer]; put = store.put; failures = []
        store.new_process()
        def interrupted(path, data, **options):
            if path == pointer:
                failures.append(dict(options))
                raise RuntimeError(self.ERROR)
            return put(path, data, **options)
        with patch.object(store, 'put', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, '^LARGE_READ_TRANSPORT_RETRY_REQUIRED$'):
                read_large(store, store.path, store.file(store.path))
        self.assertEqual(failures, [{'expected_sha': digest(original)}] * 3)
        self.assertEqual(store.data[pointer], original)
        self.assertEqual(json.loads(original)['next_offset'], CHUNK)
        store.new_process()
        self.assertEqual(read_large(store, store.path, store.file(store.path)), store.data[store.path])
        self.assertEqual(store.calls[0][0], CHUNK)
