import importlib.util
import io
import json
import pathlib
import unittest
from unittest import mock
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cloudrun" / "apps-script-post.py"

spec = importlib.util.spec_from_file_location("apps_script_post", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def open(self, req, timeout=60):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def http_error(code, location=None):
    headers = {}
    if location is not None:
        headers["Location"] = location
    return urllib.error.HTTPError(
        url="https://script.google.com/macros/s/test/exec",
        code=code,
        msg="test",
        hdrs=headers,
        fp=io.BytesIO(b""),
    )


class AppsScriptPostRetryTests(unittest.TestCase):
    def test_health_response_retries_original_post(self):
        opener = FakeOpener([
            FakeResponse({"ok": True, "service": "HUNTER_GLOBAL_BRIDGE"}),
            FakeResponse({"ok": True, "daily": {"counts": {}}}),
        ])
        with mock.patch.object(mod.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(mod.time, "sleep"):
            result = mod.post_json("https://script.google.com/macros/s/test/exec",
                                   {"op": "daily_trigger_status"}, attempts=2)
        self.assertIn("daily", result)
        self.assertEqual(opener.calls, 2)

    def test_redirect_health_response_exhaustion_fails_closed(self):
        opener = FakeOpener([http_error(302, "https://script.googleusercontent.com/one-time")])
        with mock.patch.object(mod.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(mod.urllib.request, "urlopen", return_value=FakeResponse(
                    {"ok": True, "service": "HUNTER_GLOBAL_BRIDGE"})):
            with self.assertRaisesRegex(RuntimeError, "BRIDGE_POST_RETURNED_HEALTH"):
                mod.post_json("https://script.google.com/macros/s/test/exec",
                              {"op": "read"}, attempts=1)

    def test_redirect_target_404_retries_original_post(self):
        opener = FakeOpener([
            http_error(302, "https://script.googleusercontent.com/one-time"),
            FakeResponse({"ok": True, "attempt": 2}),
        ])
        with mock.patch.object(mod.urllib.request, "build_opener", return_value=opener),              mock.patch.object(mod.urllib.request, "urlopen", side_effect=http_error(404)),              mock.patch.object(mod.time, "sleep"):
            result = mod.post_json("https://script.google.com/macros/s/test/exec", {"op": "x"}, attempts=2)
        self.assertEqual(result, {"ok": True, "attempt": 2})
        self.assertEqual(opener.calls, 2)

    def test_nonretryable_redirect_target_error_raises(self):
        opener = FakeOpener([
            http_error(302, "https://script.googleusercontent.com/one-time"),
        ])
        with mock.patch.object(mod.urllib.request, "build_opener", return_value=opener),              mock.patch.object(mod.urllib.request, "urlopen", side_effect=http_error(403)),              mock.patch.object(mod.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                mod.post_json("https://script.google.com/macros/s/test/exec", {"op": "x"}, attempts=2)

    def test_direct_success_unchanged(self):
        opener = FakeOpener([FakeResponse({"ok": True})])
        with mock.patch.object(mod.urllib.request, "build_opener", return_value=opener):
            result = mod.post_json("https://script.google.com/macros/s/test/exec", {"op": "x"}, attempts=1)
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
