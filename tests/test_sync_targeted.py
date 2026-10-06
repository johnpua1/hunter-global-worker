"""Regressions for response loss and incomplete DAILY process status."""
import os
import io
import json
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hunter-global"))
import runner


class TargetedSyncTests(unittest.TestCase):
    def test_non_json_read_uses_independent_connection_and_validates_result(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key, drive.http = 'https://script.google.com/macros/s/test/exec', 'test', Mock()
        drive.http.post.return_value.status_code = 200
        drive.http.post.return_value.content = b'<html>temporary error</html>'
        drive.http.post.return_value.json.side_effect = ValueError('not JSON')
        with patch('runner.bridge_read_response', return_value={'ok': True, 'file': {'size': 123}}) as fallback:
            self.assertEqual(drive._call('file', path='US/CURRENT_UNIVERSE.json', _attempts=1)['file']['size'], 123)
            fallback.assert_called_once()
        with patch('runner.bridge_read_response', return_value={'ok': True, 'service': 'HUNTER_GLOBAL_BRIDGE'}):
            with self.assertRaisesRegex(ValueError, 'POST_RETURNED_HEALTH'):
                drive._call('file', path='US/file', _attempts=1)

    def test_read_fallback_does_not_replay_writes(self):
        with self.assertRaisesRegex(ValueError, 'WRITE_DENIED'):
            runner.bridge_read_response('https://script.google.com/macros/s/test/exec',
                                        {'op': 'append'}, 30)

    def test_read_fallback_redirect_has_no_post_body_or_key(self):
        url = 'https://script.google.com/macros/s/test/exec'
        target = 'https://script.googleusercontent.com/response'
        error = runner.urllib.error.HTTPError(url, 302, 'redirect', {'Location': target}, io.BytesIO())
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'ok': True, 'file': None}).encode()
        opener = Mock()
        opener.open.side_effect = [error, response]
        with patch('runner.urllib.request.build_opener', return_value=opener):
            runner.bridge_read_response(url, {'op': 'file', 'key': 'test-key'}, 30)
        request = opener.open.call_args_list[1].args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertNotIn('test-key', repr(request.headers))

    def test_read_fallback_rejects_untrusted_redirect(self):
        error = runner.urllib.error.HTTPError('url', 302, 'redirect',
                    {'Location': 'https://example.com/'}, io.BytesIO())
        opener = Mock()
        opener.open.side_effect = error
        with patch('runner.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, 'REDIRECT_DENIED'):
                runner.bridge_read_response('https://script.google.com/macros/s/test/exec', {'op': 'read'}, 30)
        opener.open.assert_called_once()

    def drive(self, failure, stored):
        drive = runner.Drive.__new__(runner.Drive)
        drive._call = Mock(side_effect=failure)
        drive.file = Mock(return_value={"id": "existing"} if stored is not None else None)
        drive.read = Mock(return_value=stored)
        return drive

    def test_append_response_lost_after_commit_is_confirmed_by_content(self):
        for error in (runner.requests.ReadTimeout("lost response"),
                      RuntimeError("BRIDGE_APPEND_CONFLICT")):
            with self.subTest(error=type(error).__name__):
                drive = self.drive(error, b"verified bytes")
                self.assertEqual(drive.append("US/DAILY/day/part", b"verified bytes"),
                                 {"id": "existing"})
                drive._call.assert_called_once()

    def test_append_never_accepts_different_content_or_missing_file(self):
        for stored in (b"other writer's bytes", None):
            with self.subTest(stored=stored):
                drive = self.drive(RuntimeError("BRIDGE_APPEND_CONFLICT"), stored)
                with self.assertRaisesRegex(RuntimeError, "APPEND_CONFLICT"):
                    drive.append("US/DAILY/day/part", b"expected")

    def test_failed_readback_does_not_report_success(self):
        drive = self.drive(runner.requests.ReadTimeout("response lost"), b"expected")
        drive.read.side_effect = runner.requests.ReadTimeout("readback unavailable")
        with self.assertRaises(runner.requests.ReadTimeout):
            drive.append("US/DAILY/day/part", b"expected")

    def test_policy_rejection_is_not_reconciled(self):
        drive = self.drive(RuntimeError("BRIDGE_SCOPE_PATH_DENIED"), b"expected")
        with self.assertRaisesRegex(RuntimeError, "SCOPE_PATH_DENIED"):
            drive.append("US/DAILY/day/part", b"expected")
        drive.file.assert_not_called()

    def test_source_outage_cannot_exit_successfully(self):
        env = {"APPS_SCRIPT_WEBAPP_URL": "test", "APPS_SCRIPT_SHARED_KEY": "test",
               "HUNTER_ACTIONS_CUTOVER": "CONFIRMED"}
        with patch.dict(os.environ, env), \
                patch.object(sys, "argv", ["runner", "--mode", "auto", "--market", "US"]), \
                patch("runner._enforce_cloud_run_topology"), \
                patch("production_guard.check_at_start"), patch("runner.Drive"), \
                patch("foundation.seed_corporate_actions"), \
                patch("foundation.run_daily", return_value=[{
                    "status": "MARKET_WIDE_DATA_UNAVAILABLE", "written": 0}]):
            with self.assertRaisesRegex(RuntimeError, "DAILY_INCOMPLETE:US"):
                runner.main()


if __name__ == "__main__":
    unittest.main()
