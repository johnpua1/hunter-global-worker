import base64
import hashlib
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "hunter-global" / "runner.py"
sys.path.insert(0, str(RUNNER_PATH.parent))
spec = importlib.util.spec_from_file_location("hunter_runner_bridge_test", RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


class DriveReadResilienceTests(unittest.TestCase):
    def test_expired_404_response_restarts_from_exec_and_preserves_file_result(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key = 'https://script.google.com/macros/s/test/exec', 'key'
        old, fresh = mock.Mock(), mock.Mock()
        drive.http = old
        old.post.return_value = mock.Mock(status_code=302, headers={'Location':'https://script.googleusercontent.com/expired'})
        expired = mock.Mock(status_code=404)
        expired.raise_for_status.side_effect = runner.requests.HTTPError('HTTP 404')
        old.get.return_value = expired
        fresh.post.return_value = mock.Mock(status_code=200)
        fresh.post.return_value.json.return_value = {'ok': True, 'file': None}
        with mock.patch.object(runner.requests, 'Session', return_value=fresh), mock.patch.object(runner.time, 'sleep'):
            self.assertEqual(drive._call('file', path='US/CONTROL/DAILY_RUN_2026-10-06.json', _attempts=2), {'ok':True,'file':None})
        old.close.assert_called_once()
        self.assertEqual(fresh.post.call_args.args[0], drive.url)
        fresh.get.assert_not_called()
        expired.json.assert_not_called()

    def test_second_content_redirect_completes_reads_and_writes_without_reposting(self):
        for op, payload in [('file', {'ok': True, 'file': {'size': 123}}),
                            ('append', {'ok': True, 'file': {}, 'sha256': 'abc'}),
                            ('put', {'ok': True, 'file': {}, 'sha256': 'abc'})]:
            with self.subTest(op=op):
                drive = runner.Drive.__new__(runner.Drive)
                drive.url, drive.key, drive.http = 'https://script.google.com/macros/s/test/exec', 'test-key', mock.Mock()
                first = mock.Mock(status_code=302, content=b'', headers={'Location': 'https://script.googleusercontent.com/first'})
                second = mock.Mock(status_code=302, content=b'', headers={'Location': 'https://script.googleusercontent.com/second'})
                final = mock.Mock(status_code=200)
                final.json.return_value = payload
                drive.http.post.return_value = first
                drive.http.get.side_effect = [second, final]
                with mock.patch.object(runner, 'bridge_read_response') as fallback:
                    self.assertEqual(drive._call(op, path='HK/file.json', _attempts=1), payload)
                    fallback.assert_not_called()
                drive.http.post.assert_called_once()
                self.assertEqual(drive.http.get.call_args_list, [
                    mock.call('https://script.googleusercontent.com/first', timeout=120.0, allow_redirects=False),
                    mock.call('https://script.googleusercontent.com/second', timeout=120.0, allow_redirects=False)])
                second.json.assert_not_called()

    def test_untrusted_second_redirect_is_not_followed_and_read_recovery_is_preserved(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key, drive.http = 'https://script.google.com/macros/s/test/exec', 'key', mock.Mock()
        drive.http.post.return_value = mock.Mock(status_code=302, content=b'', headers={'Location': 'https://script.googleusercontent.com/first'})
        drive.http.get.return_value = mock.Mock(status_code=302, content=b'', headers={'Location': 'https://untrusted.example/collect'})
        payload = {'ok': True, 'file': None}
        with mock.patch.object(runner, 'bridge_read_response', return_value=payload) as fallback:
            self.assertEqual(drive._call('file', path='HK/file.json', _attempts=1), payload)
            fallback.assert_called_once_with(drive.url, {'op': 'file', 'key': 'key', 'path': 'HK/file.json'}, 120.0)
        drive.http.get.assert_called_once_with('https://script.googleusercontent.com/first', timeout=120.0, allow_redirects=False)

    def test_redirect_loop_is_bounded_before_existing_read_recovery(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key, drive.http = 'url', 'key', mock.Mock()
        loop = mock.Mock(status_code=302, content=b'', headers={'Location': 'https://script.googleusercontent.com/loop'})
        drive.http.post.return_value = drive.http.get.return_value = loop
        with mock.patch.object(runner, 'bridge_read_response', return_value={'ok': True, 'file': None}) as fallback:
            drive._call('file', path='HK/file.json', _attempts=1)
            fallback.assert_called_once()
        self.assertEqual(drive.http.get.call_count, 4)
        loop.json.assert_not_called()

    def test_unresolved_write_redirect_never_uses_read_fallback(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key, drive.http = 'url', 'key', mock.Mock()
        loop = mock.Mock(status_code=302, content=b'', headers={'Location': 'https://script.googleusercontent.com/loop'})
        drive.http.post.return_value = drive.http.get.return_value = loop
        with mock.patch.object(runner, 'bridge_read_response') as fallback:
            with self.assertRaisesRegex(ValueError, 'BRIDGE_RESPONSE_REDIRECT_UNRESOLVED:append'):
                drive._call('append', path='HK/file.json', _attempts=1)
            fallback.assert_not_called()
        self.assertEqual(drive.http.get.call_count, 4)
        drive.http.post.assert_called_once()

    def test_content_response_redirect_is_explicit_and_key_is_not_resent(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key = 'https://script.google.com/macros/s/test/exec', 'test-key'
        drive.http = mock.Mock()
        target = 'https://script.googleusercontent.com/macros/echo?one-time=response'
        drive.http.post.return_value.status_code = 302
        drive.http.post.return_value.headers = {'Location': target}
        drive.http.get.return_value.json.return_value = {'ok': True, 'file': None}
        self.assertEqual(drive._call('file', path='US/file.json'), {'ok': True, 'file': None})
        self.assertFalse(drive.http.post.call_args.kwargs['allow_redirects'])
        drive.http.get.assert_called_once_with(target, timeout=120.0, allow_redirects=False)

    def test_non_content_redirect_is_not_followed(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key, drive.http = 'url', 'key', mock.Mock()
        drive.http.post.return_value.status_code = 302
        drive.http.post.return_value.headers = {'Location': 'https://script.google.com/macros/s/test/exec'}
        with self.assertRaisesRegex(ValueError, 'BRIDGE_UNEXPECTED_REDIRECT'):
            drive._call('file', path='US/file.json', _attempts=1)
        drive.http.get.assert_not_called()

    def test_timeout_reduces_chunk_and_resumes_exact_offset(self):
        payload = b'abcdef' * 100000
        drive = runner.Drive.__new__(runner.Drive)
        drive.file = lambda path: {'size': len(payload)}
        calls = []
        def call(op, **fields):
            offset, length = fields['offset'], fields['length']
            calls.append((offset, length))
            if length > 262144:
                raise runner.requests.ReadTimeout('oversized response')
            part = payload[offset:offset + length]
            return {'data_base64': base64.b64encode(part).decode(), 'sha256': runner.digest(part),
                    'offset': offset, 'size': len(payload), 'length': len(part),
                    'eof': offset + len(part) == len(payload)}
        drive._call = call
        self.assertEqual(drive.read('HK/BASE/batch-0001.ndjson.gz'), payload)
        self.assertEqual(calls[:2], [(0, 524288), (0, 262144)])
        self.assertTrue(all(length <= 262144 for _, length in calls[1:]))

    def test_zero_length_chunk_cannot_loop_forever(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.file = lambda path: {'size': 600000}
        drive._call = mock.Mock(return_value={'offset': 0, 'size': 600000, 'data_base64': '',
                                             'length': 0, 'sha256': runner.digest(b''), 'eof': False})
        with self.assertRaisesRegex(RuntimeError, 'CHUNK_SHA_MISMATCH'):
            drive.read('US/file.json')
        drive._call.assert_called_once()

    def test_reader_preserves_connection_without_environment_lookup(self):
        drive = runner.Drive.__new__(runner.Drive)
        drive.url, drive.key = "configured-url", "configured-key"
        drive.http = mock.Mock()
        drive.folders = {"": "", "US": "US"}
        with mock.patch.dict(runner.os.environ, {}, clear=True), \
                mock.patch.object(runner.requests, "Session") as session:
            reader = drive.fork_reader()
        self.assertEqual((reader.url, reader.key), (drive.url, drive.key))
        self.assertIs(reader.http, session.return_value)
        self.assertIsNot(reader.http, drive.http)
        self.assertIsNot(reader.folders, drive.folders)

    def test_parallel_reader_sessions_close_on_failure(self):
        child = mock.Mock()
        drive = mock.Mock()
        drive.fork_reader.return_value = child
        def fail(reader, item):
            raise RuntimeError("read failed")
        with self.assertRaisesRegex(RuntimeError, "read failed"):
            list(runner.map_drive_reads(drive, fail, [1], 1))
        child.http.close.assert_called_once()

    def test_parallel_reads_bound_input_consumption_and_preserve_order(self):
        consumed = []
        drive = mock.Mock()
        drive.fork_reader.side_effect = lambda: mock.Mock()
        def source():
            for item in range(20):
                consumed.append(item)
                yield item
        results = runner.map_drive_reads(drive, lambda reader, item: item, source(), 2)
        self.assertEqual(consumed, [])
        self.assertEqual(next(results), 0)
        self.assertEqual(consumed, [0, 1])
        self.assertEqual(list(results), list(range(1, 20)))

    def test_decoded_segments_are_released_instead_of_accumulating(self):
        import tracemalloc
        drive = mock.Mock()
        drive.fork_reader.side_effect = lambda: mock.Mock()
        def read(reader, item):
            return bytearray(256 * 1024)
        tracemalloc.start()
        try:
            count = 0
            for segment in runner.map_drive_reads(drive, read, range(200), 2):
                count += 1
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(count, 200)
        self.assertLess(peak, 4 * 1024 * 1024)  # Eager storage exceeds 50 MiB.

    def test_early_close_closes_reader_sessions(self):
        readers = []
        drive = mock.Mock()
        def factory():
            reader = mock.Mock()
            readers.append(reader)
            return reader
        drive.fork_reader.side_effect = factory
        results = runner.map_drive_reads(drive, lambda reader, item: item, range(20), 2)
        self.assertEqual(next(results), 0)
        results.close()
        self.assertTrue(readers)
        for reader in readers:
            reader.http.close.assert_called_once()

    def test_sequential_transport_is_lazy(self):
        drive = object()
        operation = mock.Mock(side_effect=lambda reader, item: item)
        results = runner.map_drive_reads(drive, operation, range(20), 2)
        self.assertEqual(next(results), 0)
        self.assertEqual(operation.call_count, 1)
        results.close()

    def test_large_json_uses_bounded_read_chunks(self):
        payload = (b'{"market":"HK","securities":[' + b'{"security_id":"HK-X"},' * 30000 + b'{}]}')
        drive = runner.Drive.__new__(runner.Drive)
        calls = []

        def file_info(path):
            return {"size": len(payload)}

        def call(op, **fields):
            calls.append((op, dict(fields)))
            self.assertEqual(op, "read_chunk")
            offset = int(fields["offset"])
            length = int(fields["length"])
            part = payload[offset:offset + length]
            return {
                "data_base64": base64.b64encode(part).decode("ascii"),
                "sha256": hashlib.sha256(part).hexdigest(),
                "offset": offset,
                "length": len(part),
                "size": len(payload),
                "eof": offset + len(part) >= len(payload),
            }

        drive.file = file_info
        drive._call = call
        result = drive.read("HK/CURRENT_UNIVERSE.json")
        self.assertEqual(result, payload)
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(op == "read_chunk" for op, _ in calls))
        self.assertTrue(all(fields["length"] <= 524288 for _, fields in calls))

    def test_small_file_keeps_single_read(self):
        payload = b'{"ok":true}'
        drive = runner.Drive.__new__(runner.Drive)
        calls = []
        drive.file = lambda path: {"size": len(payload)}

        def call(op, **fields):
            calls.append(op)
            self.assertEqual(op, "read")
            return {
                "data_base64": base64.b64encode(payload).decode("ascii"),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }

        drive._call = call
        self.assertEqual(drive.read("HK/small.json"), payload)
        self.assertEqual(calls, ["read"])

    def test_retry_rebuilds_http_session_source_guard(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        retry = source[source.index("def _call"):source.index("def list", source.index("def _call"))]
        self.assertIn("self.http.close()", retry)
        self.assertIn("self.http = requests.Session()", retry)


if __name__ == "__main__":
    unittest.main()
