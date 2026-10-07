import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hunter-global'))
import runner

class SmallRangeRecoveryTests(unittest.TestCase):
    def reader(self, error=None, alter=None):
        raw=b'{"market":"HK","security_id":"HK-000632"}'
        drive=runner.Drive.__new__(runner.Drive)
        drive.file=Mock(return_value={'size':len(raw)})
        def call(op, **fields):
            if op=='read':
                self.assertEqual(fields['_attempts'],2)
                raise error or ValueError('BRIDGE_READ_FALLBACK_FAILED:ValueError')
            self.assertEqual(op,'read_chunk')
            self.assertEqual(fields['offset'],0)
            self.assertEqual(fields['length'],len(raw))
            value={'data_base64':base64.b64encode(raw).decode(), 'sha256':runner.digest(raw),
                   'size':len(raw), 'offset':0, 'length':len(raw), 'eof':True}
            value.update(alter or {})
            return value
        drive._call=Mock(side_effect=call)
        return drive,raw

    def test_failed_tiny_read_uses_range_and_preserves_exact_bytes(self):
        for error in (ValueError('fallback failed'), runner.requests.HTTPError('HTTP 503')):
            drive,raw=self.reader(error)
            self.assertEqual(drive.read('HK/REPAIR_PATCH/8d/patch.json'),raw)
            self.assertEqual([c.args[0] for c in drive._call.call_args_list],['read','read_chunk'])

    def test_bridge_permission_or_missing_file_error_is_not_bypassed(self):
        for message in ('BRIDGE_SCOPE_DENIED','BRIDGE_FILE_NOT_FOUND'):
            drive,_=self.reader(RuntimeError(message))
            with self.assertRaisesRegex(RuntimeError,message): drive.read('HK/patch.json')
            self.assertEqual(drive._call.call_count,1)

    def test_recovery_rejects_corrupt_or_mispositioned_payload(self):
        for change in ({'sha256':'bad'},{'size':900},{'offset':1},{'length':900}):
            drive,_=self.reader(alter=change)
            with self.assertRaisesRegex(RuntimeError,'MISMATCH'): drive.read('HK/patch.json')

    def test_both_paths_failing_does_not_return_empty_or_retry_same_tiny_range(self):
        drive,raw=self.reader()
        drive._call.side_effect=ValueError('transport unavailable')
        with self.assertRaises(ValueError): drive.read('HK/patch.json')
        self.assertEqual(drive._call.call_count,2)

    def test_primary_hash_failure_does_not_get_hidden_by_fallback(self):
        drive,raw=self.reader()
        drive._call.side_effect=None
        drive._call.return_value={'data_base64':base64.b64encode(raw).decode(),'sha256':'bad'}
        with self.assertRaisesRegex(RuntimeError,'SHA_MISMATCH'): drive.read('HK/patch.json')
        self.assertEqual(drive._call.call_count,1)
