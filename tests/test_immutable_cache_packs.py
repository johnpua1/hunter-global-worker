import json
import os
import re
import unittest
from unittest.mock import patch

import requests
from runner import digest
from test_incremental_inputs import Store,open_cache
from test_acceleration_deploy import AccelerationControllerTests

class ImmutableCachePacksTests(unittest.TestCase):
    def setUp(self):
        self.store=Store();self.old='US/BASE/batch-0001.ndjson.gz';self.store.data[self.old]=b'old'
        # Existing production packs have numeric rotating slots.
        with patch.dict(os.environ,{'HUNTER_CONTINUE_ONLY':'0'}):
            cache,drive=open_cache(self.store);drive.read(self.old);cache.flush()
        self.pointer=cache.pointer;self.committed=self.store.data[self.pointer]
        self.oldpack=cache.pack_path('0',cache.manifest['packs']['0']['slot'])
        self.oldbytes=self.store.data[self.oldpack]
        p=patch.dict(os.environ,{'HUNTER_CONTINUE_ONLY':'1'});p.start();self.addCleanup(p.stop)
        self.new='US/DAILY/2026-10-08/part-0001.ndjson.gz';self.store.data[self.new]=b'new'
        self.store.reads.clear();self.store.writes.clear()

    def test_numeric_pack_is_restored_and_new_pack_is_immutable_content_named(self):
        cache,drive=open_cache(self.store);drive.read(self.new)
        put=self.store.put
        with patch.object(self.store,'put',side_effect=put) as writes:cache.flush()
        calls=[c for c in writes.call_args_list if c.args[0].endswith('.zip')]
        self.assertEqual(len(calls),1);call=calls[0]
        self.assertTrue(call.kwargs['immutable'])
        self.assertTrue(call.args[0].endswith('-'+digest(call.args[1])+'.zip'))
        self.assertEqual(self.store.data[self.oldpack],self.oldbytes)
        self.store.reads.clear()
        cache,drive=open_cache(self.store)
        self.assertEqual(drive.read(self.old),b'old');self.assertEqual(drive.read(self.new),b'new')
        self.assertEqual(self.store.reads[self.old],0);self.assertEqual(self.store.reads[self.new],0)

    def test_lost_pack_ack_keeps_prior_manifest_and_pack_untouched(self):
        cache,drive=open_cache(self.store);drive.read(self.new)
        put=self.store.put
        def lost(path,data,**kwargs):
            put(path,data,**kwargs)
            if path.endswith('.zip'):raise requests.ReadTimeout('lost ACK')
        with patch.object(self.store,'put',side_effect=lost):
            with self.assertRaises(requests.ReadTimeout):cache.flush()
        self.assertEqual(self.store.data[self.pointer],self.committed)
        self.assertEqual(self.store.data[self.oldpack],self.oldbytes)
        self.assertNotEqual(cache.dirty,set())

    def test_future_flush_from_hash_slot_preserves_prior_pack(self):
        cache,drive=open_cache(self.store);drive.read(self.new);cache.flush()
        saved={p:b for p,b in self.store.data.items() if p.endswith('.zip')}
        extra='US/DAILY/2026-10-09/part-0001.ndjson.gz';self.store.data[extra]=b'extra'
        cache,drive=open_cache(self.store);drive.read(extra);cache.flush()
        self.assertTrue(all(self.store.data[p]==b for p,b in saved.items()))
        cache,drive=open_cache(self.store)
        self.assertEqual(drive.read(extra),b'extra')

    def test_hash_slot_must_match_manifest_pack_hash(self):
        cache,drive=open_cache(self.store);drive.read(self.new);cache.flush()
        doc=json.loads(self.store.data[self.pointer]);doc['packs']['0']['slot']='0'*64
        self.store.data[self.pointer]=json.dumps(doc).encode()
        with self.assertRaisesRegex(RuntimeError,'REBUILD_FORBIDDEN'):open_cache(self.store)

class PackFailureHandoffTests(AccelerationControllerTests):
    def test_exact_cache_pack_write_stack_is_recognized(self):
        m=self.m
        text='''Traceback (most recent call last):
  File "/app/incremental_inputs.py", line 256, in _flush
    writer.put(self.pack_path(key, slot), data, mime='application/octet-stream')
  File "/app/runner.py", line 524, in _continuation_write
    result = self._call(op, _attempts=1, **fields)
requests.exceptions.HTTPError: 404 Client Error: Not Found for url: https://script.googleusercontent.com/macros/echo?REDACTED'''
        with patch.object(m,'execution_source',return_value=m.pack_deployment.EXISTING['US']),patch.object(m.r,'gc',return_value=[{'textPayload':text}]),patch.object(m,'emit'):
            self.assertTrue(m.immutable_batch_failure('hunter-us-daily-r76kl','US'))
            text=text.replace('self.pack_path(key, slot)','self.pointer')
        with patch.object(m,'execution_source',return_value=m.pack_deployment.EXISTING['US']),patch.object(m.r,'gc',return_value=[{'textPayload':text}]),patch.object(m,'emit'):
            self.assertFalse(m.immutable_batch_failure('hunter-us-daily-r76kl','US'))

if __name__=='__main__':unittest.main()
