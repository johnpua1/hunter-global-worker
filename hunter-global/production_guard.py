"""Fail-closed startup configuration guard for all four Cloud Run jobs.

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
JOB_ARGS = {
    "hunter-us-daily": ["/app/runner.py", "--mode", "auto", "--market", "US"],
    "hunter-hk-daily": ["/app/runner.py", "--mode", "auto", "--market", "HK"],
    "hunter-maintenance": ["/app/maintenance.py"],
    "hunter-monthly-v2": ["/app/runner.py", "--mode", "monthly"],
}
EXPECTED_JOBS = set(JOB_ARGS)
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _read_limit(path: str) -> str:
    try:
        return Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return "UNAVAILABLE"


def resource_limits() -> tuple[str, str]:
    """Read finite limits on cgroup v2 or Cloud Run's v1 controller mounts.

    Paths are not fingerprint inputs: the same limits have the same identity
    across layouts. Preserve the actual quota; never round it to a nominal CPU.
    """
    for root in ("/sys/fs/cgroup", "/sys/fs/cgroup/unified"):
        cpu = _read_limit(root + "/cpu.max")
        memory = _read_limit(root + "/memory.max")
        if cpu != "UNAVAILABLE" and memory != "UNAVAILABLE":
            break
    else:
        for controller in ("cpu,cpuacct", "cpu", "cpuacct,cpu"):
            root = "/sys/fs/cgroup/" + controller
            quota = _read_limit(root + "/cpu.cfs_quota_us")
            period = _read_limit(root + "/cpu.cfs_period_us")
            if quota != "UNAVAILABLE" and period != "UNAVAILABLE":
                cpu = quota + " " + period
                break
        else:
            raise ValueError("RESOURCE_LIMIT_UNAVAILABLE")
        memory = _read_limit("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if memory == "UNAVAILABLE":
        raise ValueError("RESOURCE_LIMIT_UNAVAILABLE")
    fields = cpu.split()
    if (len(fields) != 2 or not all(re.fullmatch(r"[0-9]+", x) for x in fields)
            or not re.fullmatch(r"[0-9]+", memory)):
        raise ValueError("RESOURCE_LIMIT_INVALID")
    quota, period = map(int, fields)
    memory_bytes = int(memory)
    if min(quota, period, memory_bytes) <= 0:
        raise ValueError("RESOURCE_LIMIT_INVALID")
    return f"{quota} {period}", str(memory_bytes)


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
        return  # Local tests and administrative CLI do not enroll production.
    stamp = dt.datetime.now(MYT).isoformat(timespec="seconds")
    try:
        if job not in EXPECTED_JOBS:
            raise ValueError("UNEXPECTED_JOB")
        if list(sys.argv) != JOB_ARGS[job]:
            raise ValueError("ENTRYPOINT_MISMATCH")
        if os.environ.get("CLOUD_RUN_TASK_COUNT") != "1":
            raise ValueError("TASK_COUNT_MISMATCH")
        if os.environ.get("HUNTER_ACTIONS_CUTOVER") != "CONFIRMED":
            raise ValueError("SINGLE_WRITER_NOT_CONFIRMED")
        if not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("HUNTER_SOURCE_SHA", "")):
            raise ValueError("SOURCE_SHA_UNPINNED")
        key = os.environ.get("APPS_SCRIPT_SHARED_KEY", "")
        if not key or key != key.strip():
            raise ValueError("BRIDGE_KEY_INVALID")
        if not re.fullmatch(r"https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec",
                            os.environ.get("APPS_SCRIPT_WEBAPP_URL", "")):
            raise ValueError("BRIDGE_URL_INVALID")
        account = _service_account_email()
        account_name = "hunter-monthly" if job == "hunter-monthly-v2" else job
        if account != account_name + "@rgs-hunter-global.iam.gserviceaccount.com":
            raise ValueError("SERVICE_ACCOUNT_MISMATCH")
        cpu, memory = resource_limits()
        actual = fingerprint(job=job, environ=dict(os.environ), argv=list(sys.argv),
                             service_account=account, cpu_limit=cpu, memory_limit=memory)
        probe = os.environ.get("HUNTER_CONFIG_PROBE", "")
        if probe not in ("", "ENROLL", "VERIFY"):
            raise ValueError("CONFIG_PROBE_INVALID")
        if probe == "ENROLL":
            # A per-execution override only. Stop before creating a Drive client.
            LOG.info("HUNTER_CONFIG_ENROLL worker=%s myt=%s sha256=%s", job, stamp, actual)
            raise SystemExit(0)
        expected = os.environ.get("HUNTER_CONFIG_SHA256", "")
        if not HEX_SHA256.fullmatch(expected) or actual != expected:
            raise ValueError("HASH_MISMATCH")
        LOG.info("HUNTER_CONFIG_OK worker=%s myt=%s sha256=%s", job, stamp, actual)
        if probe == "VERIFY":
            raise SystemExit(0)
    except Exception as exc:
        LOG.error("HUNTER_CONFIG_BLOCKED worker=%s myt=%s reason=%s",
                  job, stamp, safe_error_summary(exc))
        raise RuntimeError("HUNTER_CONFIG_BLOCKED") from None
