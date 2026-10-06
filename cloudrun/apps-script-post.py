#!/usr/bin/env python3
"""Robust Apps Script Web App JSON POST client.

Apps Script ContentService responds to doPost with a redirect to a one-time
script.googleusercontent.com URL. urllib's default redirect handling can turn
that flow into a terminal 404. This client captures the redirect explicitly and
GETs the one-time response URL.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decode_response(response) -> dict:
    raw = response.read()
    result = json.loads(raw.decode("utf-8"))
    if result == {"ok": True, "service": "HUNTER_GLOBAL_BRIDGE"}:
        raise ValueError("BRIDGE_POST_RETURNED_HEALTH")
    if not isinstance(result, dict):
        raise ValueError("BRIDGE_POST_INVALID_RESPONSE")
    return result


def post_json(url: str, payload: dict, attempts: int = 6) -> dict:
    opener = urllib.request.build_opener(NoRedirect)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    last: Exception | None = None

    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(req, timeout=60) as response:
                return _decode_response(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if not location:
                    raise RuntimeError("APPS_SCRIPT_REDIRECT_WITHOUT_LOCATION") from exc
                # ContentService's redirect target is a one-time response URL.
                # That URL can expire or race deployment propagation and return
                # 404/5xx. In that case retry the original /exec POST instead of
                # failing the whole control-plane call.
                try:
                    with urllib.request.urlopen(location, timeout=60) as response:
                        return _decode_response(response)
                except urllib.error.HTTPError as redirect_exc:
                    last = redirect_exc
                    if redirect_exc.code not in (404, 408, 429, 500, 502, 503, 504):
                        raise
                except (urllib.error.URLError, TimeoutError, ValueError) as redirect_exc:
                    last = redirect_exc
                if attempt + 1 < attempts:
                    time.sleep(min(5, 1 + attempt))
                    continue
                break
            last = exc
            # Apps Script /exec itself can also briefly return retryable
            # failures during deployment propagation.
            if exc.code not in (404, 408, 429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(min(5, 1 + attempt))

    raise RuntimeError(f"APPS_SCRIPT_POST_FAILED:{last!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--op", required=True)
    parser.add_argument("--path")
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    url = os.environ["BRIDGE_URL"]
    key = os.environ["LEGACY_KEY"]
    payload = {"op": args.op, "key": key}
    if args.path is not None:
        payload["path"] = args.path

    result = post_json(url, payload)
    if args.raw:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
