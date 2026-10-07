import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hunter-global'))
import runner


class ReadResponse404Tests(unittest.TestCase):
    def reader(self, code=404, redirect=True):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url = 'https://script.google.com/macros/s/test/exec'
        drive.key = 'test-key'
        drive.http = Mock()
        response = Mock(status_code=code, content=b'')
        response.raise_for_status.side_effect = runner.requests.HTTPError('HTTP ' + str(code))
        drive.http.post.return_value = (Mock(status_code=302, headers={
            'Location': 'https://script.googleusercontent.com/expired'}) if redirect else response)
        drive.http.get.return_value = response
        return drive

    def test_expired_chunk_uses_fresh_independent_read_at_identical_offset(self):
        drive = self.reader()
        raw = b'{"market":"HK"}'
        payload = {'ok': True, 'offset': 128, 'length': len(raw), 'size': 1024,
                   'data_base64': base64.b64encode(raw).decode(),
                   'sha256': runner.digest(raw), 'eof': False}
        fields = {'path': 'HK/REPAIR_PATCH/8d/patch.json', 'offset': 128, 'length': 256}
        with patch.object(runner, 'bridge_read_response', return_value=payload) as fallback:
            self.assertEqual(drive._call('read_chunk', _attempts=1, **fields), payload)
        fallback.assert_called_once_with(drive.url, {'op': 'read_chunk', 'key': drive.key, **fields}, 30.0)
        drive.http.post.assert_called_once()

    def test_canonical_404_and_auth_failures_do_not_enter_recovery(self):
        for code, redirect in ((404, False), (401, True), (403, True)):
            drive = self.reader(code, redirect)
            with patch.object(runner, 'bridge_read_response') as fallback:
                with self.assertRaises(runner.requests.HTTPError):
                    drive._call('read_chunk', path='HK/patch.json', _attempts=1)
                fallback.assert_not_called()

    def test_writes_never_use_independent_read_recovery(self):
        for op in ('put', 'append', 'folder'):
            drive = self.reader()
            with patch.object(runner, 'bridge_read_response') as fallback:
                with self.assertRaises(runner.requests.HTTPError):
                    drive._call(op, path='HK/patch.json', _attempts=1)
                fallback.assert_not_called()

    def test_real_bridge_missing_or_scope_denial_is_not_accepted(self):
        for error in ('FILE_NOT_FOUND', 'SCOPE_DENIED'):
            drive = self.reader()
            with patch.object(runner, 'bridge_read_response', return_value={'ok': False, 'error': error}):
                with self.assertRaisesRegex(RuntimeError, 'BRIDGE_' + error):
                    drive._call('read_chunk', path='HK/patch.json', _attempts=1)

    def test_failed_fallback_remains_failure_without_unbounded_retries(self):
        drive = self.reader()
        with patch.object(runner, 'bridge_read_response', side_effect=ValueError('unavailable')) as fallback:
            with self.assertRaisesRegex(ValueError, 'BRIDGE_READ_FALLBACK_FAILED'):
                drive._call('read_chunk', path='HK/patch.json', _attempts=1)
        fallback.assert_called_once()

    def test_recovered_chunk_still_rejects_corrupt_content(self):
        drive = self.reader()
        drive.file = Mock(return_value={'size': 300000})
        bad = {'ok': True, 'offset': 0, 'length': 3, 'size': 300000,
               'data_base64': base64.b64encode(b'bad').decode(), 'sha256': 'wrong', 'eof': False}
        with patch.object(runner, 'bridge_read_response', return_value=bad):
            with self.assertRaisesRegex(RuntimeError, 'CHUNK_SHA_MISMATCH'):
                drive.read('HK/patch.json')


if __name__ == '__main__':
    unittest.main()
