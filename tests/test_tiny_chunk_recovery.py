import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hunter-global'))
import runner

class TinyChunkTests(unittest.TestCase):
    def reader(self, raw, fail, corrupt=False):
        drive=runner.Drive.__new__(runner.Drive)
        drive.file=Mock(return_value={'size':len(raw)})
        calls=[]
        def call(op,**f):
            if op=='read': raise ValueError('ContentService 404')
            offset,length=f['offset'],f['length'];calls.append((offset,length))
            if fail(offset,length):raise ValueError('ContentService 404')
            part=raw[offset:offset+length]
            return dict(offset=offset,length=len(part),size=len(raw),eof=offset+len(part)==len(raw),
                        data_base64=base64.b64encode(part).decode(),sha256='bad' if corrupt else runner.digest(part))
        drive._call=call
        return drive,calls

    def test_378_byte_file_recovers_by_splitting_not_repeating_whole_file(self):
        raw=bytes(range(126))*3
        drive,calls=self.reader(raw,lambda o,n:n>189)
        self.assertEqual(drive.read('US/DAILY/2025-03-17/part-0001.ndjson.gz'),raw)
        self.assertEqual(calls,[(0,378),(0,189),(189,189)])

    def test_unavailable_file_stops_at_bounded_floor_without_partial_success(self):
        drive,calls=self.reader(b'x'*378,lambda o,n:True)
        with self.assertRaises(ValueError):drive.read('US/file.gz')
        self.assertEqual(calls,[(0,378),(0,189),(0,94),(0,64)])

    def test_tail_failure_shrinks_actual_remaining_request_preserving_offset(self):
        raw=b'x'*700
        drive,calls=self.reader(raw,lambda o,n:(o==0 and n>350) or (o==350 and n>175))
        self.assertEqual(drive.read('US/file.gz'),raw)
        self.assertEqual(calls,[(0,700),(0,350),(350,350),(350,175),(525,175)])

    def test_corrupt_recovered_piece_cannot_be_accepted(self):
        drive,calls=self.reader(b'x'*378,lambda o,n:n>189,corrupt=True)
        with self.assertRaisesRegex(RuntimeError,'SHA_MISMATCH'):drive.read('US/file.gz')
        self.assertEqual(calls,[(0,378),(0,189)])

    def test_bridge_denial_is_not_retried_as_smaller_chunk(self):
        drive,calls=self.reader(b'x'*378,lambda o,n:False)
        drive._call=Mock(side_effect=RuntimeError('BRIDGE_SCOPE_DENIED'))
        with self.assertRaisesRegex(RuntimeError,'SCOPE_DENIED'):drive.read('US/file.gz')
        self.assertEqual(drive._call.call_count,1)
