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
