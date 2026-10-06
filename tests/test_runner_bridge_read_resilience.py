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
            runner.map_drive_reads(drive, fail, [1], 1)
        child.http.close.assert_called_once()

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
