import base64
import gzip
import json
import unittest
from unittest.mock import patch

from test_incremental_inputs import Store, open_cache
from runner import compact, digest
from durable_reads import read_large, CHUNK


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
