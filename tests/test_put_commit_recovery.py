import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hunter-global'))
import runner


class PutCommitRecoveryTests(unittest.TestCase):
    path='HK/CONTROL/DAILY_CHECKPOINT.json'
    payload=runner.compact({'market':'HK','last_completed_date':'2026-10-06',
                            'phase2_completed_date':'2026-10-06',
                            'phase2_completed_at_myt':'2026-10-07T16:23:12+08:00'})

    def drive(self,error,actual=None):
        drive=runner.Drive.__new__(runner.Drive)
        drive._call=Mock(side_effect=error)
        drive.file=Mock(return_value={'id':'committed-file'})
        drive.read=Mock(return_value=self.payload if actual is None else actual)
        return drive

    def test_committed_cas_is_confirmed_after_ambiguous_response(self):
        for error in (RuntimeError('BRIDGE_STALE_WRITE'),ValueError('non-JSON'),
                      runner.requests.ReadTimeout(),runner.requests.ConnectionError()):
            drive=self.drive(error)
            self.assertEqual(drive.put(self.path,self.payload,expected_sha='old'),{'id':'committed-file'})
            drive._call.assert_called_once()
            self.assertEqual(drive._call.call_args.kwargs['expected_sha256'],'old')

    def test_stale_different_bytes_remain_conflict_without_overwrite(self):
        error=RuntimeError('BRIDGE_STALE_WRITE');drive=self.drive(error,b'{"different":true}')
        with self.assertRaises(RuntimeError) as caught:
            drive.put(self.path,self.payload,expected_sha='old')
        self.assertIs(caught.exception,error);drive._call.assert_called_once()

    def test_readback_failure_does_not_confirm_commit(self):
        for error in (runner.requests.ReadTimeout(),ValueError('invalid'),RuntimeError('BRIDGE_READ_SHA_MISMATCH')):
            drive=self.drive(RuntimeError('BRIDGE_STALE_WRITE'));drive.read.side_effect=error
            with self.assertRaisesRegex(RuntimeError,'STALE_WRITE'):
                drive.put(self.path,self.payload,expected_sha='old')

    def test_missing_file_does_not_confirm_commit(self):
        drive=self.drive(RuntimeError('BRIDGE_STALE_WRITE'));drive.file.return_value=None
        with self.assertRaisesRegex(RuntimeError,'STALE_WRITE'):
            drive.put(self.path,self.payload,expected_sha='old')
        drive.read.assert_not_called()

    def test_auth_scope_and_integrity_errors_are_not_reconciled(self):
        for error in (RuntimeError('BRIDGE_SCOPE_DENIED'),RuntimeError('BRIDGE_AUTH_FAILED'),
                      RuntimeError('BRIDGE_SHA_MISMATCH'),runner.requests.HTTPError('HTTP 403')):
            drive=self.drive(error)
            with self.assertRaises(type(error)):
                drive.put(self.path,self.payload,expected_sha='old')
            drive.file.assert_not_called();drive.read.assert_not_called()

    def test_non_cas_and_immutable_writes_keep_existing_failure_semantics(self):
        for kwargs in ({},{'expected_sha':'old','immutable':True}):
            drive=self.drive(RuntimeError('BRIDGE_STALE_WRITE'))
            with self.assertRaisesRegex(RuntimeError,'STALE_WRITE'):
                drive.put(self.path,self.payload,**kwargs)
            drive.file.assert_not_called()

    def test_actual_transport_retry_then_stale_confirms_persisted_checkpoint(self):
        drive=runner.Drive.__new__(runner.Drive)
        drive.url='https://script.google.com/macros/s/test/exec';drive.key='test'
        drive.http=Mock();fresh=Mock()
        first=Mock(status_code=200);first.json.side_effect=ValueError('lost response')
        second=Mock(status_code=200);second.json.return_value={'ok':False,'error':'STALE_WRITE'}
        drive.http.post.return_value=first;fresh.post.return_value=second
        drive.file=Mock(return_value={'id':'committed-file'});drive.read=Mock(return_value=self.payload)
        with patch('runner.requests.Session',return_value=fresh),patch('runner.time.sleep'):
            result=drive.put(self.path,self.payload,expected_sha='old')
        self.assertEqual(result,{'id':'committed-file'})
        self.assertEqual(fresh.post.call_count,1)
        self.assertEqual(fresh.post.call_args.kwargs['json']['expected_sha256'],'old')

    def test_acknowledged_wrong_hash_is_not_hidden_by_readback(self):
        drive=self.drive(None);drive._call.side_effect=None
        drive._call.return_value={'file':{},'sha256':'wrong'}
        with self.assertRaisesRegex(RuntimeError,'WRITE_SHA_MISMATCH'):
            drive.put(self.path,self.payload,expected_sha='old')
        drive.file.assert_not_called();drive.read.assert_not_called()
