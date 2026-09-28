"""Read-only configuration drift check for the three Cloud Run jobs.

The expected fingerprint is enrolled after a production image and job template
are verified. Never log the inputs: one of them is the Bridge credential.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


LOG = logging.getLogger("hunter.guard")
MYT = ZoneInfo("Asia/Kuala_Lumpur")
ENV_KEYS = (
    "APPS_SCRIPT_SHARED_KEY",
    "APPS_SCRIPT_WEBAPP_URL",
    "FETCH_WORKERS",
    "HUNTER_ACTIONS_CUTOVER",
    "HUNTER_SOURCE_SHA",
)
EXPECTED_JOBS = {"hunter-us-daily", "hunter-hk-daily", "hunter-maintenance"}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _read_limit(path: str) -> str:
    try:
        return Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return "UNAVAILABLE"


def _service_account_email() -> str:
    response = requests.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/email",
        headers={"Metadata-Flavor": "Google"},
        timeout=3,
    )
    response.raise_for_status()
    return response.text.strip()


def fingerprint(*, job: str, environ: dict[str, str], argv: list[str],
                service_account: str, cpu_limit: str, memory_limit: str) -> str:
    # The expected-hash variable is deliberately excluded. Hashing the Bridge
    # key detects rotation without ever exposing it in logs or in the image.
    document = {
        "schema": 1,
        "job": job,
        "argv": argv,
        "env": {key: environ.get(key, "") for key in ENV_KEYS},
        "task_count": environ.get("CLOUD_RUN_TASK_COUNT", ""),
        "service_account": service_account,
        "cpu_max": cpu_limit,
        "memory_max": memory_limit,
    }
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def safe_error_summary(exc: Exception) -> str:
    """Only emit a machine-readable error code, never HTTP bodies or keys."""
    code = str(exc).split(":", 1)[0]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", code):
        code = "UNCLASSIFIED"
    return type(exc).__name__ + ":" + code


def check_at_start() -> None:
    job = os.environ.get("CLOUD_RUN_JOB", "")
    if not job:
        return  # Local tests and administrative CLI use the same modules.
    stamp = dt.datetime.now(MYT).isoformat(timespec="seconds")
    try:
        if job not in EXPECTED_JOBS:
            raise ValueError("UNEXPECTED_JOB")
        if not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("HUNTER_SOURCE_SHA", "")):
            raise ValueError("SOURCE_SHA_UNPINNED")
        actual = fingerprint(
            job=job,
            environ=dict(os.environ),
            argv=list(sys.argv),
            service_account=_service_account_email(),
            cpu_limit=_read_limit("/sys/fs/cgroup/cpu.max"),
            memory_limit=_read_limit("/sys/fs/cgroup/memory.max"),
        )
        expected = os.environ.get("HUNTER_CONFIG_SHA256", "")
        if not HEX_SHA256.fullmatch(expected) or actual != expected:
            LOG.error("HUNTER_CONFIG_DRIFT worker=%s myt=%s reason=HASH_MISMATCH "
                      "expected_sha256=%s actual_sha256=%s; continuing",
                      job, stamp, expected or "UNREGISTERED", actual)
        else:
            LOG.info("HUNTER_CONFIG_OK worker=%s myt=%s sha256=%s", job, stamp, actual)
    except Exception as exc:
        # A temporary metadata failure must never stop market processing.
        LOG.error("HUNTER_CONFIG_DRIFT worker=%s myt=%s reason=HASH_UNAVAILABLE:%s; "
                  "continuing", job, stamp, type(exc).__name__)
