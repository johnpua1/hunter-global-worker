"""One Maintenance writer across all entry points, using Bridge atomic CAS.

Ownership never expires by time alone: a slow live execution keeps its lock.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import uuid
from contextlib import contextmanager

from production_guard import _get_json, safe_error_summary
from runner import compact, digest, now_myt

LOG = logging.getLogger("hunter.maintenance")
LOCK_PATH = "US/CONTROL/MAINTENANCE_LOCK.json"
EXECUTION = re.compile(r"hunter-maintenance-[a-z0-9]+")


def execution_finished(execution):
    if not isinstance(execution, str) or not EXECUTION.fullmatch(execution):
        raise RuntimeError("MAINTENANCE_LOCK_OWNER_INVALID")
    name = "projects/rgs-hunter-global/locations/us-central1/jobs/hunter-maintenance/executions/" + execution
    token = _get_json(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        {"Metadata-Flavor": "Google"}, "MAINTENANCE_LOCK_TOKEN").get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("MAINTENANCE_LOCK_TOKEN_MISSING")
    doc = _get_json("https://run.googleapis.com/v2/" + name,
                    {"Authorization": "Bearer " + token}, "MAINTENANCE_LOCK_EXECUTION")
    if doc.get("name") not in (name, name.replace("projects/rgs-hunter-global/", "projects/1018303980503/")):
        raise RuntimeError("MAINTENANCE_LOCK_EXECUTION_MISMATCH")
    return bool(doc.get("completionTime"))


def read_lock(drive):
    try:
        raw = drive.read(LOCK_PATH)
    except RuntimeError as exc:
        if str(exc) != "BRIDGE_FILE_NOT_FOUND":
            raise
        return None, None
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("schema") != 1 or "owner" not in doc:
        raise RuntimeError("MAINTENANCE_LOCK_INVALID")
    owner = doc["owner"]
    if owner is not None and (
        not isinstance(owner, dict)
        or not isinstance(owner.get("execution"), str)
        or not EXECUTION.fullmatch(owner["execution"])
        or type(owner.get("attempt")) is not int or owner["attempt"] < 0
        or not isinstance(owner.get("token"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", owner["token"])
    ):
        raise RuntimeError("MAINTENANCE_LOCK_OWNER_INVALID")
    return raw, owner


def cas_lock(drive, previous, owner):
    data = compact({"schema": 1, "owner": owner, "updated_at_myt": now_myt()})
    # put_fast omits expected_sha when None; creation MUST send explicit null.
    response = drive._call("put", path=LOCK_PATH,
                           data_base64=base64.b64encode(data).decode("ascii"),
                           sha256=digest(data), mime="application/json", immutable=False,
                           expected_sha256=digest(previous) if previous is not None else None)
    if response.get("sha256") != digest(data):
        raise RuntimeError("MAINTENANCE_LOCK_WRITE_UNVERIFIED")


def acquire(drive, execution, attempt, token, finished=execution_finished):
    if not isinstance(execution, str) or not EXECUTION.fullmatch(execution) or type(attempt) is not int or attempt < 0:
        raise RuntimeError("MAINTENANCE_LOCK_IDENTITY_INVALID")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise RuntimeError("MAINTENANCE_LOCK_TOKEN_INVALID")
    wanted = {"execution": execution, "attempt": attempt, "token": token}
    for _ in range(8):
        raw, owner = read_lock(drive)
        if owner == wanted:
            return True  # Reconcile a committed CAS whose response was lost.
        if owner:
            if owner["execution"] == execution:
                # Cloud Run's next attempt follows termination of the prior one.
                available = attempt > owner["attempt"]
            else:
                available = finished(owner["execution"])
            if not available:
                LOG.info("MAINTENANCE_SKIPPED_ACTIVE_OWNER execution=%s owner=%s attempt=%s",
                         execution, owner["execution"], owner["attempt"])
                return False
        try:
            cas_lock(drive, raw, wanted)
        except RuntimeError as exc:
            if str(exc) != "BRIDGE_STALE_WRITE":
                raise
            continue
        if read_lock(drive)[1] != wanted:
            raise RuntimeError("MAINTENANCE_LOCK_READBACK_MISMATCH")
        return True
    raise RuntimeError("MAINTENANCE_LOCK_CAS_EXHAUSTED")


def release(drive, execution, attempt, token):
    wanted = {"execution": execution, "attempt": attempt, "token": token}
    raw, owner = read_lock(drive)
    if owner != wanted:
        raise RuntimeError("MAINTENANCE_LOCK_OWNERSHIP_LOST")
    try:
        cas_lock(drive, raw, None)
    except RuntimeError as exc:
        if str(exc) != "BRIDGE_STALE_WRITE":
            raise
        # A lost release response may be followed by the next writer acquiring.
        if read_lock(drive)[1] == wanted:
            raise


@contextmanager
def maintenance_slot(drive):
    execution = os.environ.get("CLOUD_RUN_EXECUTION", "")
    attempt_text = os.environ.get("CLOUD_RUN_TASK_ATTEMPT", "")
    if not re.fullmatch(r"[0-9]+", attempt_text):
        raise RuntimeError("MAINTENANCE_LOCK_ATTEMPT_INVALID")
    attempt, token = int(attempt_text), uuid.uuid4().hex
    held = acquire(drive, execution, attempt, token)
    if not held:
        yield False
        return
    LOG.info("MAINTENANCE_LOCK_ACQUIRED execution=%s attempt=%s", execution, attempt)
    failed = False
    try:
        yield True
    except BaseException:
        failed = True
        raise
    finally:
        try:
            release(drive, execution, attempt, token)
            LOG.info("MAINTENANCE_LOCK_RELEASED execution=%s attempt=%s", execution, attempt)
        except Exception as exc:
            LOG.error("MAINTENANCE_LOCK_RELEASE_FAILED reason=%s", safe_error_summary(exc))
            if not failed:
                raise


def verify_lock(drive):
    """Production probe: touch only the lock, never run maintenance business work."""
    execution = os.environ.get("CLOUD_RUN_EXECUTION", "")
    if execution_finished(execution):
        raise RuntimeError("MAINTENANCE_LOCK_PROBE_EXECUTION_COMPLETED")
    with maintenance_slot(drive) as held:
        if not held:
            raise RuntimeError("MAINTENANCE_LOCK_PROBE_BUSY")
        attempt = int(os.environ["CLOUD_RUN_TASK_ATTEMPT"])
        if acquire(drive, execution, attempt, uuid.uuid4().hex):
            raise RuntimeError("MAINTENANCE_LOCK_PROBE_DUPLICATE_ACCEPTED")
    if read_lock(drive)[1] is not None:
        raise RuntimeError("MAINTENANCE_LOCK_PROBE_RELEASE_UNCONFIRMED")
    LOG.info("MAINTENANCE_LOCK_VERIFIED execution=%s", execution)
