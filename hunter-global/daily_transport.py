"""Bridge transport only. No legacy worker entry point or market pipeline."""
from __future__ import annotations

import argparse

import concurrent.futures

import datetime as dt

import gzip

import hashlib

import json

import logging

import math

import os

import random

import re

import sys

import time

import threading

import urllib.error

import urllib.request

from dataclasses import dataclass

from typing import Any

from urllib.parse import quote, urlsplit

from zoneinfo import ZoneInfo

import requests

ROOT = "https://www.googleapis.com/drive/v3/files"

UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

TZ = ZoneInfo("Asia/Kuala_Lumpur")

LOG = logging.getLogger("hunter")

MARKETS = ("US", "HK")

class _NoBridgeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def bridge_read_response(url: str, payload: dict, timeout: float) -> dict:
    """Independent, read-only retry after an unusable ContentService response."""
    if payload.get("op") not in {"file", "list", "read", "read_chunk", "read_verified_chunk", "source_inventory"}:
        raise ValueError("BRIDGE_READ_FALLBACK_WRITE_DENIED")
    opener = urllib.request.build_opener(_NoBridgeRedirect)
    from execution_budget import timeout as budget_timeout
    request = urllib.request.Request(url, data=compact(payload), method="POST",
                                     headers={"Content-Type": "application/json"})
    for _ in range(4):
        try:
            with opener.open(request, timeout=budget_timeout(timeout)) as response:
                return json.loads(response.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers.get("Location", "")
            target = urlsplit(location)
            if target.scheme != "https" or target.hostname != "script.googleusercontent.com":
                raise ValueError("BRIDGE_READ_FALLBACK_REDIRECT_DENIED") from None
            # The response hop is a GET without credentials or the POST body.
            request = urllib.request.Request(location, method="GET")
    raise ValueError("BRIDGE_READ_FALLBACK_REDIRECT_LIMIT")

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def compact(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

def lines_gz(rows: list[dict]) -> bytes:
    # Apps Script produces gzip, and its manifest checks the compressed bytes.
    payload = b"\n".join(compact(row) for row in rows) + (b"\n" if rows else b"")
    return gzip.compress(payload, mtime=0)

def now_myt() -> str:
    return dt.datetime.now(TZ).isoformat(timespec="seconds")

def retry_http(session, method: str, url: str, **kwargs):
    retryable = {429, 500, 502, 503, 504}
    last_error = None
    attempts = max(1, int(os.getenv("HUNTER_HTTP_RETRY_ATTEMPTS", "5")))
    timeout = max(3.0, float(os.getenv("HUNTER_HTTP_TIMEOUT_SECONDS", "45")))
    for attempt in range(attempts):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
            time.sleep(min(30, 2**attempt + random.random()))
            continue

        # Non-retryable HTTP failures (for example 401/403/404) are
        # deterministic for this request and must fail immediately. Only
        # throttling/transient server statuses are retried.
        if response.status_code not in retryable:
            response.raise_for_status()
            return response

        last_error = requests.HTTPError(f"HTTP {response.status_code}", response=response)
        if attempt == attempts - 1:
            raise last_error
        time.sleep(min(30, 2**attempt + random.random()))

    if last_error is not None:
        raise last_error
    raise AssertionError("unreachable")

class Drive:
    """Restricted Apps Script transport for this Hunter worker."""

    def __init__(self):
        needed = ("APPS_SCRIPT_WEBAPP_URL", "APPS_SCRIPT_SHARED_KEY")
        missing = [key for key in needed if not os.environ.get(key)]
        if missing:
            raise RuntimeError("BRIDGE_AUTH_MISSING:" + ",".join(missing))
        self.url = os.environ["APPS_SCRIPT_WEBAPP_URL"]
        if not (self.url.startswith("https://script.google.com/macros/s/") and self.url.endswith("/exec")):
            raise RuntimeError("BRIDGE_URL_INVALID")
        self.key = os.environ["APPS_SCRIPT_SHARED_KEY"]
        self.http = requests.Session()
        self.folders: dict[str, str] = {"": ""}
        self._read_state = {"guard": threading.Lock(), "locks": {}, "memory": {}}
        self._continuation_state = {'guard': threading.Lock(), 'locks': {}, 'memory': {}}

    def fork_reader(self):
        """Keep this connection's configuration with an independent HTTP session."""
        reader = object.__new__(type(self))
        reader.url, reader.key = self.url, self.key
        reader.http = requests.Session()
        reader.folders = dict(self.folders)
        if hasattr(self, '_read_state'):
            reader._read_state = self._read_state
        if hasattr(self, '_continuation_state'):
            reader._continuation_state = self._continuation_state
        return reader

    def health(self):
        expected = {"ok": True, "service": "HUNTER_GLOBAL_BRIDGE"}
        for attempt in range(8):
            try:
                response = self.http.get(self.url, timeout=60)
                response.raise_for_status()
                result = response.json()
                if result != expected:
                    raise ValueError("BRIDGE_HEALTH_RESPONSE_SHAPE")
                LOG.info("bridge health ok")
                return
            except (requests.RequestException, ValueError):
                if attempt == 7:
                    raise
                # Apps Script redirects health GETs to short-lived
                # script.googleusercontent.com URLs.  A stale redirect can
                # transiently return 404; rebuild the session and retry the
                # original /exec URL instead of accepting or caching it.
                self.http.close()
                self.http = requests.Session()
                time.sleep(min(30, 2 ** attempt + random.random()))
        raise AssertionError("unreachable")

    def _call(self, op: str, *, _attempts: int | None = None, **fields) -> dict:
        from execution_budget import timeout as budget_timeout
        request = {"op": op, "key": self.key, **fields}
        attempts = max(1, _attempts if _attempts is not None else
                       int(os.getenv("HUNTER_BRIDGE_ATTEMPTS", "8")))
        for attempt in range(attempts):
            try:
                reading = op in ("file", "list", "read", "read_chunk", "read_verified_chunk", "source_inventory")
                started = time.monotonic()
                if reading:
                    timeout = float(os.getenv("HUNTER_BRIDGE_READ_TIMEOUT_SECONDS", "30"))
                    LOG.info("BRIDGE_READ_START op=%s path=%s attempt=%d timeout=%.1f",
                             op, fields.get("path", ""), attempt + 1, timeout)
                else:
                    timeout = float(os.getenv("HUNTER_BRIDGE_WRITE_TIMEOUT_SECONDS", "120"))
                # ContentService redirects to a one-time response resource.
                # Handle that hop explicitly, as the control-plane client does;
                # never silently turn an intermediate /exec redirect into GET.
                response = self.http.post(self.url, json=request, timeout=budget_timeout(timeout),
                                          allow_redirects=False)
                response_hop = False
                for hop in range(4):
                    if response.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = response.headers.get("Location", "")
                    target = urlsplit(location)
                    if target.scheme != "https" or target.hostname != "script.googleusercontent.com":
                        if hop == 0:
                            raise ValueError("BRIDGE_UNEXPECTED_REDIRECT:" + op)
                        # Preserve the independent read recovery for an unusable
                        # response hop, but never follow an untrusted target.
                        break
                    # Every response hop is GET-only; do not replay a write or
                    # forward the shared key when ContentService redirects again.
                    from continuation import enabled as continue_only
                    if not reading and op in {'put', 'append'} and continue_only():
                        from write_recovery import response_get
                        response = response_get(self.http, location, timeout)
                    else:
                        response = self.http.get(location, timeout=budget_timeout(timeout), allow_redirects=False)
                    response_hop = True
                try:
                    # A 404 on ContentService's disposable response URL is not
                    # a Drive FILE_NOT_FOUND. Reissue this read through the
                    # independent client, from /exec, just like non-JSON hops.
                    # Canonical endpoint errors and all writes retain their
                    # existing error handling; no write enters this fallback.
                    if reading and response_hop and response.status_code == 404:
                        raise ValueError("BRIDGE_READ_RESPONSE_EXPIRED:" + op)
                    response.raise_for_status()
                    if response.status_code in (301, 302, 303, 307, 308):
                        raise ValueError("BRIDGE_RESPONSE_REDIRECT_UNRESOLVED:" + op)
                    result = response.json()
                except ValueError:
                    if op not in {"file", "list", "read", "read_chunk", "read_verified_chunk", "source_inventory"}:
                        raise
                    LOG.warning("BRIDGE_NON_JSON op=%s path=%s status=%s bytes=%d",
                                op, fields.get("path", ""), response.status_code,
                                len(response.content))
                    try:
                        result = bridge_read_response(self.url, request, budget_timeout(timeout))
                    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                        LOG.warning("BRIDGE_READ_FALLBACK_ERROR op=%s path=%s offset=%s length=%s status=%s type=%s",
                                    op, fields.get("path", ""), fields.get("offset"),
                                    fields.get("length"), getattr(exc, "code", None), type(exc).__name__)
                        raise ValueError("BRIDGE_READ_FALLBACK_FAILED:" + type(exc).__name__) from None
                    LOG.info("BRIDGE_READ_FALLBACK_RESPONSE op=%s path=%s", op, fields.get("path", ""))
                if not isinstance(result, dict):
                    raise ValueError("BRIDGE_RESPONSE_NOT_OBJECT:" + op)
                # Apps Script can rarely return the doGet health payload to a
                # POST route during a transient redirect/session anomaly. Never
                # accept it as operation data; reset the HTTP session and retry.
                if result == {"ok": True, "service": "HUNTER_GLOBAL_BRIDGE"}:
                    self.http.close()
                    self.http = requests.Session()
                    raise ValueError("BRIDGE_POST_RETURNED_HEALTH:" + op)
                if not result.get("ok"):
                    if op == "source_inventory" and str(result.get("error", "")) in {
                            "INVENTORY_HTTP_429", "INVENTORY_HTTP_500", "INVENTORY_HTTP_502",
                            "INVENTORY_HTTP_503", "INVENTORY_HTTP_504"}:
                        raise ValueError("INVENTORY_TRANSIENT_FAILURE")
                    raise RuntimeError("BRIDGE_" + str(result.get("error", "UNKNOWN")))
                required = {"read": ("data_base64", "sha256"),
                            "source_inventory": ("data_base64", "sha256"),
                            "read_chunk": ("data_base64", "sha256", "offset", "length", "size", "eof"),
                            "read_verified_chunk": ("data_base64", "sha256", "offset", "length", "size", "eof",
                                                    "file_sha256", "compressed_sha256", "revision", "encoding"),
                            "put": ("file", "sha256"), "append": ("file", "sha256"),
                            "list": ("files",), "file": ("file",), "folder": ("folder",)}
                if not all(field in result for field in required[op]):
                    raise ValueError("BRIDGE_RESPONSE_SHAPE:" + op + ":" + ",".join(sorted(result)))
                if reading:
                    LOG.info("BRIDGE_READ_DONE op=%s path=%s attempt=%d seconds=%.1f",
                             op, fields.get("path", ""), attempt + 1, time.monotonic() - started)
                return result
            except (requests.RequestException, ValueError) as exc:
                if attempt == attempts - 1:
                    raise
                # A failed ContentService redirect can leave a stale pooled
                # connection to script.googleusercontent.com. Rebuild the
                # session so the next attempt starts again from the canonical
                # Apps Script /exec endpoint.
                self.http.close()
                self.http = requests.Session()
                LOG.warning("BRIDGE_REQUEST_RETRY op=%s path=%s attempt=%d type=%s",
                            op, fields.get("path", ""), attempt + 1, type(exc).__name__)
                time.sleep(min(30, 2 ** min(attempt, 4) + random.random()))
        raise AssertionError("unreachable")

    def list(self, parent_id: str, name: str | None = None) -> list[dict]:
        return self._call("list", path=parent_id, name=name)["files"]

    def source_inventory(self, market):
        import base64
        result = self._call("source_inventory", path=market)
        packed = base64.b64decode(result["data_base64"], validate=True)
        if digest(packed) != result["sha256"]:
            raise RuntimeError("INVENTORY_SHA_MISMATCH")
        return json.loads(gzip.decompress(packed))

    def folder(self, path: str, create: bool = False) -> str:
        path = path.strip("/")
        if path in self.folders:
            return path
        self._call("folder", path=path, create=create)
        self.folders[path] = path
        return path

    def file(self, path: str) -> dict | None:
        return self._call("file", path=path.strip("/")).get("file")

    def read_known(self, path: str, info: dict) -> bytes:
        """Reuse the complete source inventory; InputCache verifies its fingerprint."""
        return self.read(path, _info=info)

    def read_checkpoint_piece(self, path: str) -> bytes:
        """Read one manifest-named immutable chunk without a metadata round trip."""
        import base64
        match = re.fullmatch(r'(US|HK)/CONTROL/READ_CACHE/[a-f0-9]{64}/part-([a-f0-9]{64})\.gz', path)
        if not match:
            raise RuntimeError('READ_CHECKPOINT_PIECE_PATH_INVALID')
        state = self._continuation_state
        with state['guard']:
            lock = state['locks'].setdefault(path, threading.RLock())
        with lock:
            if path in state['memory']:
                return state['memory'][path]
            result = self._call('read', path=path, _attempts=2)
            raw = base64.b64decode(result['data_base64'], validate=True)
            if len(raw) > 262144 or digest(raw) != match[2] or result['sha256'] != match[2]:
                raise RuntimeError('LARGE_READ_COMPRESSED_HASH_MISMATCH')
            state['memory'][path] = raw
            return raw

    def read(self, path: str, *, _info=None) -> bytes:
        from continuation import enabled as continue_only
        if not continue_only() or not hasattr(self, '_continuation_state'):
            return self._read(path, _info=_info)
        state = self._continuation_state
        path = path.strip('/')
        with state['guard']:
            lock = state['locks'].setdefault(path, threading.RLock())
        with lock:
            if path not in state['memory']:
                state['memory'][path] = self._read(path, _info=_info)
            return state['memory'][path]

    def _read(self, path: str, *, _info=None) -> bytes:
        import base64
        clean = path.strip("/")
        info = self.file(clean) if _info is None else _info
        # Apps Script ContentService becomes unreliable for multi-MB JSON
        # responses because the redirected googleusercontent response can sit
        # idle long enough to hit the worker's read timeout. Use bounded Drive
        # range reads for any non-trivial file instead of one giant response.
        chunk_threshold = max(64_000, int(os.getenv("HUNTER_BRIDGE_CHUNK_THRESHOLD_BYTES", "262144")))
        expected = int(info.get("size") or 0) if info else 0
        if (os.getenv('HUNTER_READ_CHECKPOINTS') == '1' and expected > 262144
                and clean.startswith(('US/', 'HK/'))
                and ('/PHASE2/' in clean or '/CONTROL/INCREMENTAL_CACHE/' in clean
                     or '/CONTROL/PHASE2_RESUME/' in clean or clean.endswith('/CURRENT_UNIVERSE.json'))):
            from durable_reads import read_large
            try:
                return read_large(self, clean, info)
            except (requests.RequestException, ValueError) as exc:
                raise RuntimeError('LARGE_READ_TRANSPORT_RETRY_REQUIRED') from exc
        if expected <= chunk_threshold:
            try:
                # A confirmed nonempty file can use the independent range API
                # after bounded transport retries, including for tiny JSON.
                result = self._call("read", path=clean, _attempts=2 if expected > 0 else None)
            except (requests.RequestException, ValueError):
                if expected <= 0:
                    raise
                LOG.warning("BRIDGE_READ_RANGE_FALLBACK path=%s bytes=%d", clean, expected)
            else:
                data = base64.b64decode(result["data_base64"], validate=True)
                if digest(data) != result["sha256"]:
                    raise RuntimeError("BRIDGE_READ_SHA_MISMATCH:" + path)
                return data
        expected = int(info["size"])
        chunk_size = min(expected, max(64_000, min(1_000_000, int(os.getenv("HUNTER_BRIDGE_CHUNK_BYTES", "524288")))))
        offset = 0
        chunks = []
        while offset < expected:
            try:
                result = self._call("read_chunk", path=clean, offset=offset,
                                    length=min(chunk_size, expected - offset), _attempts=2)
            except (requests.RequestException, ValueError):
                # Retry the same offset with a smaller response, instead of
                # repeatedly requesting a chunk that the Bridge cannot serve.
                # A small file can fail ContentService too. The old 64 KB
                # floor made its range fallback repeat the entire failed
                # response and then abort (observed on a 378-byte US file).
                # Shrink the actual remaining request, including a short tail.
                requested = min(chunk_size, expected - offset)
                if requested <= 64:
                    raise
                chunk_size = max(64, requested // 2)
                LOG.warning("BRIDGE_READ_REDUCE_CHUNK path=%s offset=%d bytes=%d",
                            clean, offset, chunk_size)
                continue
            if int(result["offset"]) != offset or int(result["size"]) != expected:
                raise RuntimeError("BRIDGE_READ_CHUNK_POSITION_MISMATCH:" + path)
            data = base64.b64decode(result["data_base64"], validate=True)
            if (not data or len(data) > min(chunk_size, expected - offset) or
                    len(data) != int(result["length"]) or digest(data) != result["sha256"]):
                raise RuntimeError("BRIDGE_READ_CHUNK_SHA_MISMATCH:" + path)
            chunks.append(data)
            offset += len(data)
            if result["eof"] and offset != expected:
                raise RuntimeError("BRIDGE_READ_CHUNK_EARLY_EOF:" + path)
        data = b"".join(chunks)
        if len(data) != expected:
            raise RuntimeError("BRIDGE_READ_CHUNK_SIZE_MISMATCH:" + path)
        return data

    def json(self, path: str) -> Any:
        return json.loads(self.read(path))

    def _verify_pack_write(self, path: str, content: bytes):
        """Read the stored pack's full SHA through the existing revision-bound API.

        The Bridge hashes every persisted byte, independently of the put ACK.
        Returning a small verified sample avoids downloading and checkpointing
        the entire ZIP just to compare it with bytes already in this process.
        """
        import base64
        info = self.file(path)
        if (not info or not info.get('revision') or info.get('id') != path
                or int(info.get('size', -1)) != len(content)):
            raise RuntimeError('PACK_WRITE_IDENTITY_MISMATCH')
        length = min(4096, len(content))
        proof = self._call('read_verified_chunk', path=path, revision=info['revision'],
                           offset=0, length=length, _attempts=2)
        encoded = base64.b64decode(proof['data_base64'], validate=True)
        sample = gzip.decompress(encoded)
        if (proof['revision'] != info['revision'] or proof['size'] != len(content)
                or proof['offset'] != 0 or proof['length'] != length
                or proof['encoding'] != 'gzip' or proof['eof'] != (length == len(content))
                or proof['compressed_sha256'] != digest(encoded)
                or proof['sha256'] != digest(sample) or sample != content[:length]
                or proof['file_sha256'] != digest(content)):
            raise RuntimeError('PACK_WRITE_READBACK_MISMATCH')
        current = self.file(path)
        if (not current or current.get('id') != path
                or current.get('revision') != info['revision']
                or int(current.get('size', -1)) != len(content)):
            raise RuntimeError('PACK_WRITE_REVISION_CHANGED')
        LOG.info('INPUT_PACK_WRITE_VERIFIED path=%s bytes=%d proof_bytes=%d',
                 path, len(content), length)

    def put(self, path: str, content: bytes, mime: str = "application/json", immutable=False, expected_sha=None):
        import base64
        fields = {"path": path.strip("/"), "data_base64": base64.b64encode(content).decode("ascii"),
                  "sha256": digest(content), "mime": mime, "immutable": immutable}
        if expected_sha is not None:
            fields["expected_sha256"] = expected_sha
        from continuation import enabled as continue_only
        if continue_only():
            # Mutable/append outcomes remain fail-closed. Immutable requests
            # may recover via the Bridge's locked create-once operation.
            result = self._continuation_write('put', **fields)
            if result['sha256'] != digest(content):
                raise RuntimeError('BRIDGE_WRITE_SHA_MISMATCH:' + path)
            if hasattr(self, '_continuation_state'):
                self._continuation_state['memory'][path.strip('/')] = content
            return result['file']
        try:
            result = self._call("put", **fields)
        except (requests.Timeout, requests.ConnectionError, ValueError, RuntimeError) as exc:
            # A CAS write may commit before its response is lost. Replaying
            # that request then correctly reports STALE_WRITE. Reconcile only
            # the exact desired bytes; never overwrite a competing writer.
            if (expected_sha is None or immutable or
                    isinstance(exc, RuntimeError) and str(exc) != "BRIDGE_STALE_WRITE"):
                raise
            try:
                info = self.file(path)
                identical = info is not None and digest(self.read(path)) == fields["sha256"]
            except (requests.RequestException, ValueError, RuntimeError):
                identical = False
            if not identical:
                raise exc
            LOG.info("PUT_COMMIT_CONFIRMED path=%s", path)
            return info
        if result["sha256"] != digest(content):
            raise RuntimeError("BRIDGE_WRITE_SHA_MISMATCH:" + path)
        if (len(content) > 262144 and re.fullmatch(
                r'(US|HK)/CONTROL/INCREMENTAL_CACHE/pack-[0-9]{5,}-[01]\.zip', fields['path'])):
            try:
                self._verify_pack_write(fields['path'], content)
            except (requests.RequestException, ValueError) as exc:
                raise RuntimeError('LARGE_READ_TRANSPORT_RETRY_REQUIRED') from exc
        elif digest(self.read(path)) != digest(content):
            raise RuntimeError("DRIVE_READBACK_MISMATCH:" + path)
        return result["file"]

    def put_fast(self, path: str, content: bytes, mime: str = "application/json", expected_sha=None):
        """CAS write with Bridge SHA confirmation, without an immediate full-file readback."""
        import base64
        from continuation import enabled as continue_only
        if continue_only():
            return self.put(path, content, mime=mime, expected_sha=expected_sha)
        fields = {"path": path.strip("/"), "data_base64": base64.b64encode(content).decode("ascii"),
                  "sha256": digest(content), "mime": mime, "immutable": False}
        if expected_sha is not None:
            fields["expected_sha256"] = expected_sha
        result = self._call("put", **fields)
        if result["sha256"] != digest(content):
            raise RuntimeError("BRIDGE_WRITE_SHA_MISMATCH:" + path)
        return result["file"]

    def append(self, path: str, content: bytes, mime: str = "application/json"):
        """Create once; reconcile an ambiguous response by exact-byte readback."""
        import base64
        from continuation import enabled as continue_only
        if continue_only():
            result = self._continuation_write('append', path=path.strip('/'),
                                data_base64=base64.b64encode(content).decode('ascii'),
                                sha256=digest(content), mime=mime)
            if result['sha256'] != digest(content):
                raise RuntimeError('BRIDGE_WRITE_SHA_MISMATCH:' + path)
            if hasattr(self, '_continuation_state'):
                self._continuation_state['memory'][path.strip('/')] = content
            return result['file']
        try:
            result = self._call("append", path=path.strip("/"),
                                data_base64=base64.b64encode(content).decode("ascii"),
                                sha256=digest(content), mime=mime)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if isinstance(exc, RuntimeError) and str(exc) != "BRIDGE_APPEND_CONFLICT":
                raise
            # The original POST may have committed before its response timed
            # out. Never overwrite or allocate another segment on this path.
            try:
                info = self.file(path)
                identical = info is not None and digest(self.read(path)) == digest(content)
            except (requests.RequestException, ValueError, RuntimeError):
                identical = False
            if not identical:
                raise exc
            LOG.info("APPEND_COMMIT_CONFIRMED path=%s", path)
            return info
        if result["sha256"] != digest(content) or digest(self.read(path)) != digest(content):
            raise RuntimeError("APPEND_READBACK_MISMATCH:" + path)
        return result["file"]


    def _continuation_write(self, op, **fields):
        from write_recovery import immutable_put, transient
        state = getattr(self, '_continuation_state', None)
        if state is not None and state.get('write_uncertain'):
            raise RuntimeError('CONTINUATION_WRITE_OUTCOME_UNKNOWN')
        recoverable = immutable_put(op, fields)
        try:
            for attempt in range(3 if recoverable else 1):
                LOG.info('BRIDGE_WRITE_START op=%s path=%s immutable=%s attempt=%d',
                         op, fields.get('path', ''), recoverable, attempt + 1)
                try:
                    result = self._call(op, _attempts=1, **fields)
                except (requests.RequestException, ValueError) as exc:
                    if not recoverable or not transient(exc):
                        raise
                    if attempt == 2:
                        raise RuntimeError('IMMUTABLE_WRITE_TRANSPORT_RETRY_REQUIRED') from exc
                    LOG.warning('IMMUTABLE_WRITE_ACK_RECOVERY path=%s attempt=%d type=%s',
                                fields.get('path', ''), attempt + 1, type(exc).__name__)
                    self.http.close()
                    self.http = requests.Session()
                    time.sleep(attempt + 1)
                    continue
                if result['sha256'] != fields['sha256']:
                    raise RuntimeError('BRIDGE_WRITE_SHA_MISMATCH')
                LOG.info('BRIDGE_WRITE_ACK op=%s path=%s attempt=%d',
                         op, fields.get('path', ''), attempt + 1)
                return result
        except BaseException:
            if state is not None:
                state['write_uncertain'] = True
            raise


    def append_repairs(self, market: str, batch: int, statuses: list[dict], reasons: dict):
        flags = {"FETCH_FAILED", "DATA_SUSPECT", "IDENTITY_REVIEW"}
        additions = [
            {"category": flag, "market": market, "security_id": entry["security_id"],
             "batch": batch, "problem": reasons.get(entry["security_id"], flag) if flag == "FETCH_FAILED" else flag,
             "attempted_fixes": [], "recorded_at_myt": now_myt()}
            for entry in statuses for flag in entry["status"] if flag in flags
        ]
        if not additions:
            return
        path = "REPAIR_QUEUE.json"
        while True:
            raw = self.read(path)
            document = json.loads(raw)
            if not isinstance(document, dict) or not isinstance(document.get("items"), list):
                raise RuntimeError("REPAIR_QUEUE_INVALID")
            existing = {(x.get("market"), x.get("security_id"), x.get("batch"), x.get("category"))
                        for x in document["items"] if isinstance(x, dict)}
            new = [x for x in additions if (x["market"], x["security_id"], x["batch"], x["category"]) not in existing]
            if not new:
                return
            document["items"].extend(new)
            try:
                self.put(path, compact(document), expected_sha=digest(raw))
                LOG.info("repair queue market=%s batch=%04d added=%d", market, batch, len(new))
                return
            except RuntimeError as exc:
                if "BRIDGE_STALE_WRITE" not in str(exc):
                    raise
                time.sleep(1 + random.random())
