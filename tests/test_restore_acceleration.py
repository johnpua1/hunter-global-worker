import base64
import contextvars
import gzip
import os
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from durable_reads import _restore_pieces
from runner import Drive, digest

ROOT='US/CONTROL/READ_CACHE/'+'a'*64+'/'

class RestoreAccelerationTests(unittest.TestCase):
    def setUp(self):
        p=patch.dict(os.environ,{'HUNTER_CONTINUE_ONLY':'1'})
        p.start(); self.addCleanup(p.stop)
        self.state={'guard':threading.Lock(),'locks':{},'memory':{}}
        self.pieces=[]; self.data={}; self.expected=[]
        for i in range(24):
            data=bytes([i])*1024
            raw=gzip.compress(data,mtime=0)
            self.pieces.append({'offset':i*1024,'length':1024,'sha256':digest(data),'compressed_sha256':digest(raw)})
            self.data[ROOT+'part-'+digest(raw)+'.gz']=raw
            self.expected.append(data)
        owner=self
        self.running=0; self.peak=0; self.started=[]; self.sessions=[]
        self.guard=threading.Lock(); self.fail=None; self.delay=.005
        class Reader:
            def __init__(self):self.http=Mock();owner.sessions.append(self.http)
            def fork_reader(self):return Reader()
            def read_checkpoint_piece(self,path):
                with owner.guard:
                    owner.running+=1;owner.peak=max(owner.peak,owner.running);owner.started.append(path)
                try:
                    if owner.fail:owner.fail(path)
                    time.sleep(owner.delay)
                    return owner.data[path]
                finally:
                    with owner.guard:owner.running-=1
            def read(self,path):raise AssertionError('metadata/read path forbidden')
        self.reader=Reader()

    def test_parallel_restore_preserves_order_and_reads_each_piece_once(self):
        self.assertEqual(_restore_pieces(self.reader,ROOT,self.pieces,self.state,'US/pack'),self.expected)
        self.assertGreater(self.peak,1);self.assertLessEqual(self.peak,4)
        self.assertEqual(Counter(self.started),Counter(self.data.keys()))
        for session in self.sessions[1:]:session.close.assert_called_once()

    def test_two_concurrent_packs_share_global_four_request_limit(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(_restore_pieces,self.reader,ROOT,self.pieces,self.state,'US/pack'+str(i)) for i in range(2)]
            for f in futures:self.assertEqual(f.result(),self.expected)
        self.assertEqual(self.peak,4)

    def test_failure_cancels_queued_work_instead_of_prefetching_all_pieces(self):
        first=ROOT+'part-'+self.pieces[0]['compressed_sha256']+'.gz'
        def fail(path):
            if path==first:raise RuntimeError('transport stopped')
        self.fail=fail
        with self.assertRaisesRegex(RuntimeError,'transport stopped'):
            _restore_pieces(self.reader,ROOT,self.pieces,self.state,'US/pack')
        self.assertLessEqual(len(self.started),4)
        for session in self.sessions[1:]:session.close.assert_called_once()

    def test_corrupt_piece_never_produces_a_restored_file(self):
        self.data[next(iter(self.data))]=b'corrupt'
        with self.assertRaisesRegex(RuntimeError,'HASH_MISMATCH'):
            _restore_pieces(self.reader,ROOT,self.pieces,self.state,'US/pack')

    def test_checkpoint_budget_context_is_preserved_in_workers(self):
        from execution_budget import _saving, saving
        observed=[]
        self.fail=lambda path:observed.append(_saving.get())
        with saving():_restore_pieces(self.reader,ROOT,self.pieces[:4],self.state,'US/pack')
        self.assertEqual(observed,[True]*4)

    def test_direct_piece_read_uses_no_file_metadata_or_source_read(self):
        d=Drive.__new__(Drive)
        d._continuation_state={'guard':threading.Lock(),'locks':{},'memory':{}}
        path,raw=next(iter(self.data.items()))
        with patch.object(d,'file',side_effect=AssertionError('metadata')),patch.object(d,'read',side_effect=AssertionError('read')),patch.object(d,'_call',return_value={'data_base64':base64.b64encode(raw).decode(),'sha256':digest(raw)}) as call:
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(d.read_checkpoint_piece,[path]*8)),[raw]*8)
            call.assert_called_once_with('read',path=path,_attempts=2)

    def test_direct_piece_rejects_other_paths_and_bad_hash(self):
        d=Drive.__new__(Drive)
        d._continuation_state={'guard':threading.Lock(),'locks':{},'memory':{}}
        with patch.object(d,'_call') as call:
            with self.assertRaisesRegex(RuntimeError,'PATH_INVALID'):d.read_checkpoint_piece('US/BASE/batch-0001.ndjson.gz')
            call.assert_not_called()
        path,raw=next(iter(self.data.items()))
        with patch.object(d,'_call',return_value={'data_base64':base64.b64encode(b'corrupt').decode(),'sha256':digest(raw)}):
            with self.assertRaisesRegex(RuntimeError,'HASH_MISMATCH'):d.read_checkpoint_piece(path)
        self.assertEqual(d._continuation_state['memory'],{})

if __name__=='__main__':unittest.main()
