import copy
import os
import unittest
from unittest.mock import patch

import requests
from incremental_inputs import incremental_inputs
from test_incremental_inputs import Store, open_cache
from runner import compact

class NoCacheRebuildTests(unittest.TestCase):
    def setUp(self):
        p=patch.dict(os.environ,{'HUNTER_CONTINUE_ONLY':'1','HUNTER_INCREMENTAL_INPUTS':'1'})
        p.start(); self.addCleanup(p.stop)
        self.store=Store(); self.path='US/BASE/batch-0001.ndjson.gz'
        self.store.data[self.path]=b'saved-input'
        cache,drive=open_cache(self.store); drive.read(self.path); cache.flush()
        self.pointer=cache.pointer
        self.pack=cache.pack_path('0',cache.manifest['packs']['0']['slot'])
        self.original=self.store.data[self.pointer]
        self.store.reads.clear(); self.store.writes.clear()

    def test_transient_pack_failure_keeps_manifest_and_never_reads_original(self):
        cache,drive=open_cache(self.store)
        before=copy.deepcopy(cache.manifest)
        read=self.store.read
        def fail(path):
            if path==self.pack:raise requests.ReadTimeout('response lost')
            return read(path)
        with patch.object(self.store,'read',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'INPUT_CACHE_REBUILD_FORBIDDEN'):drive.read(self.path)
        self.assertEqual(cache.manifest,before)
        self.assertEqual(cache.loaded_pack_versions,{})
        self.assertEqual(self.store.reads[self.path],0)
        self.assertEqual(cache.misses,0)
        self.assertEqual(self.store.writes,[])

    def test_corrupt_pack_is_not_rebuilt_from_original(self):
        self.store.data[self.pack]=b'broken'
        cache,drive=open_cache(self.store)
        with self.assertRaisesRegex(RuntimeError,'REBUILD_FORBIDDEN'):drive.read(self.path)
        self.assertEqual(self.store.reads[self.path],0)
        self.assertEqual(self.store.data[self.pointer],self.original)
        self.assertEqual(self.store.writes,[])

    def test_invalid_manifest_cannot_be_silently_discarded(self):
        self.store.data[self.pointer]=b'{invalid-json'
        with self.assertRaisesRegex(RuntimeError,'REBUILD_FORBIDDEN'):open_cache(self.store)
        self.assertEqual(self.store.reads[self.path],0)
        self.assertEqual(self.store.writes,[])

    def test_entry_hash_mismatch_does_not_fall_through_to_source(self):
        cache,drive=open_cache(self.store)
        cache.manifest['entries'][self.path]['sha256']='a'*64
        with self.assertRaisesRegex(RuntimeError,'REBUILD_FORBIDDEN'):drive.read(self.path)
        self.assertEqual(self.store.reads[self.path],0)
        self.assertEqual(cache.misses,0)

    def test_failure_skips_final_cache_flush(self):
        self.store.data[self.pack]=b'broken'
        with self.assertRaisesRegex(RuntimeError,'REBUILD_FORBIDDEN'):
            with incremental_inputs(self.store,'US') as drive:drive.read(self.path)
        self.assertEqual(self.store.writes,[])
        self.assertEqual(self.store.data[self.pointer],self.original)

    def test_saved_pack_success_reads_no_original_and_reuses_memory(self):
        cache,drive=open_cache(self.store)
        self.assertEqual(drive.read(self.path),b'saved-input')
        self.assertEqual(drive.read(self.path),b'saved-input')
        self.assertEqual(self.store.reads[self.path],0)
        self.assertEqual(self.store.reads[self.pack],1)
        self.assertFalse(cache.blocked)

    def test_new_input_is_still_allowed_without_rereading_old_original(self):
        new='US/DAILY/2026-10-08/part-0001.ndjson.gz'
        self.store.data[new]=b'new-day'
        cache,drive=open_cache(self.store)
        self.assertEqual(drive.read(new),b'new-day')
        self.assertEqual(drive.read(self.path),b'saved-input')
        self.assertEqual(self.store.reads[new],1)
        self.assertEqual(self.store.reads[self.path],0)

if __name__=='__main__':unittest.main()
