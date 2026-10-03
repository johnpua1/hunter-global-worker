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
import time
from decimal import Decimal
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


def _get_json(url: str, headers: dict[str, str], label: str) -> dict:
    for attempt in range(4):
        try:
            response = requests.get(url, headers=headers, timeout=(3, 10), allow_redirects=False)
        except requests.RequestException:
            if attempt == 3:
                raise ValueError(label + "_UNAVAILABLE") from None
        else:
            if response.status_code == 200:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError(label + "_INVALID")
                return result
            if response.status_code not in (404, 429, 500, 502, 503, 504) or attempt == 3:
                raise ValueError(label + "_HTTP_" + str(response.status_code))
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def resource_limits() -> tuple[str, str]:
    """Use this immutable execution's declared limits, not host cgroup quota.

    The per-execution probe timeout and attempt number are deliberately excluded.
    Requires only run.executions.get on this worker's own job.
    """
    job = os.environ.get("CLOUD_RUN_JOB", "")
    execution = os.environ.get("CLOUD_RUN_EXECUTION", "")
    if job not in EXPECTED_JOBS or not re.fullmatch(re.escape(job) + r"-[a-z0-9]+", execution):
        raise ValueError("EXECUTION_ID_INVALID")
    name = ("projects/rgs-hunter-global/locations/us-central1/jobs/" + job +
            "/executions/" + execution)
    token_doc = _get_json(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        {"Metadata-Flavor": "Google"}, "METADATA_TOKEN")
    token = token_doc.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("METADATA_TOKEN_MISSING")
    doc = _get_json("https://run.googleapis.com/v2/" + name,
                    {"Authorization": "Bearer " + token}, "EXECUTION_CONFIG")
    # The API may canonicalize the project ID to its numeric project number.
    allowed_names = {name, name.replace("projects/rgs-hunter-global/", "projects/1018303980503/")}
    if doc.get("name") not in allowed_names:
        raise ValueError("EXECUTION_ID_MISMATCH")
    if doc.get("taskCount") != 1 or doc.get("parallelism") != 1:
        raise ValueError("EXECUTION_TOPOLOGY_MISMATCH")
    containers = doc.get("template", {}).get("containers", [])
    if len(containers) != 1:
        raise ValueError("EXECUTION_CONTAINER_MISMATCH")
    limits = containers[0].get("resources", {}).get("limits", {})
    cpu, memory = str(limits.get("cpu", "")), str(limits.get("memory", ""))
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?m?", cpu):
        raise ValueError("RESOURCE_LIMIT_INVALID")
    cpu_value = Decimal(cpu[:-1]) / 1000 if cpu.endswith("m") else Decimal(cpu)
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]i|[kKMGT])?", memory)
    if cpu_value <= 0 or not match:
        raise ValueError("RESOURCE_LIMIT_INVALID")
    factors = {None: 1, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
               "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    memory_value = Decimal(match[1]) * factors[match[2]]
    if memory_value <= 0 or memory_value != int(memory_value):
        raise ValueError("RESOURCE_LIMIT_INVALID")
    return format(cpu_value.normalize(), "f"), str(int(memory_value))


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
        "schema": 2,
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
        LOG.info("HUNTER_CONFIG_DETAIL worker=%s myt=%s cpu=%s memory=%s source=%s actual_sha256=%s",
                 job, stamp, cpu, memory, os.environ["HUNTER_SOURCE_SHA"], actual)
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
