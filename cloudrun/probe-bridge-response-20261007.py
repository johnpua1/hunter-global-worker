"""Read-only comparison of the failing Bridge response path; no body/URL logs."""
import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import time
from urllib.parse import urlsplit

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hunter-global"))
from runner import Drive, parse_lines_gz


class TraceSession(requests.Session):
    def request(self, method, url, **kwargs):
        start = time.monotonic()
        response = super().request(method, url, **kwargs)
        print(json.dumps({"event": "HTTP_RESPONSE", "method": method,
                          "host": urlsplit(url).hostname, "status": response.status_code,
                          "content_type": response.headers.get("Content-Type"),
                          "bytes": len(response.content),
                          "redirect_host": urlsplit(response.headers.get("Location", "")).hostname,
                          "seconds": round(time.monotonic() - start, 2)}), flush=True)
        return response


class Reader(Drive):
    def _call(self, op, **fields):
        if op not in {"file", "read", "read_chunk"}:
            raise RuntimeError("READ_ONLY")
        return super()._call(op, **fields)


def main():
    os.environ.update(HUNTER_BRIDGE_ATTEMPTS="1", HUNTER_BRIDGE_READ_TIMEOUT_SECONDS="45",
                      HUNTER_BRIDGE_WRITE_TIMEOUT_SECONDS="45")
    reader = Reader()
    if '--verify-fix' in sys.argv:
        for market, path in [('US', 'US/CURRENT_UNIVERSE.json'),
                             ('HK', 'HK/BASE/batch-0001.ndjson.gz')]:
            start = time.monotonic()
            raw = reader.read(path)
            count = len(json.loads(raw)['securities']) if market == 'US' else len(parse_lines_gz(raw))
            print(json.dumps({'event': 'FULL_READ_PASS', 'market': market,
                              'count': count, 'seconds': round(time.monotonic() - start, 2)}), flush=True)
        class NonJSONResponse:
            status_code = 200
            content = b''
            def raise_for_status(self): pass
            def json(self): raise ValueError('SIMULATED_NON_JSON_RESPONSE')
        class NonJSONSession:
            def post(self, *args, **kwargs): return NonJSONResponse()
        reader.http = NonJSONSession()
        start = time.monotonic()
        doc = reader.json('US/CURRENT_UNIVERSE.json')
        print(json.dumps({'event': 'REAL_READ_WITH_INJECTED_PRIMARY_RESPONSE_FAILURE_PASS',
                          'securities': len(doc['securities']),
                          'seconds': round(time.monotonic() - start, 2)}), flush=True)
        return
    reader.http = TraceSession()
    path = "US/CURRENT_UNIVERSE.json"
    for length in (524288, 64000):
        try:
            result = reader._call("read_chunk", path=path, offset=0, length=length)
            data = base64.b64decode(result["data_base64"], validate=True)
            assert hashlib.sha256(data).hexdigest() == result["sha256"]
            print(json.dumps({"client": "requests", "length": length, "result": "PASS"}), flush=True)
        except Exception as exc:
            print(json.dumps({"client": "requests", "length": length,
                              "result": "FAIL", "type": type(exc).__name__}), flush=True)
    spec = importlib.util.spec_from_file_location("post", ROOT / "cloudrun/apps-script-post.py")
    post = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(post)
    try:
        start = time.monotonic()
        result = post.post_json(reader.url, {"op": "read_chunk", "key": reader.key,
                                "path": path, "offset": 0, "length": 524288}, attempts=2)
        data = base64.b64decode(result["data_base64"], validate=True)
        assert hashlib.sha256(data).hexdigest() == result["sha256"]
        print(json.dumps({"client": "urllib", "length": len(data), "result": "PASS",
                          "seconds": round(time.monotonic() - start, 2)}), flush=True)
    except Exception as exc:
        print(json.dumps({"client": "urllib", "result": "FAIL", "type": type(exc).__name__}), flush=True)


if __name__ == "__main__":
    main()
