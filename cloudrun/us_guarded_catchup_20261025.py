#!/usr/bin/env python3
"""One-time US-only Hunter recovery: 2026-10-22 and 2026-10-23.

Run in Cloud Shell from an authenticated, current main checkout.
Never edits checkpoint, BASE, previous ranks, GitHub triggers, HK or scheduler.
Updates ONLY existing hunter-us-daily image and manually executes at most twice.
Every day is accepted only after its authoritative Drive receipts confirm it.
"""
from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "rgs-hunter-global"
REGION = "us-central1"
JOB = "hunter-us-daily"
TARGET = "2026-10-23"
PREV = "2026-10-21"
DAY = "2026-10-22"
FAILED = "hunter-us-daily-x6jgj"
MYT = ZoneInfo("Asia/Kuala_Lumpur")


def shell(*args: str, capture: bool = True) -> str:
    result = subprocess.run(list(args), cwd=ROOT, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            check=True)
    return result.stdout if capture else ""


def module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gcloud(*args: str) -> str:
    return shell("gcloud", *args, f"--project={PROJECT}")


def log_failure(execution: str = FAILED) -> None:
    """Read only actual error records, never cache INFO noise or credentials."""
    if not re.fullmatch(r"hunter-us-daily-[a-z0-9]+", execution):
        raise RuntimeError("US_DIAGNOSTIC_EXECUTION_ID_INVALID")
    query = ('resource.type="cloud_run_job" AND '
             'resource.labels.job_name="' + JOB + '" AND '
             'labels."run.googleapis.com/execution_name"="' + execution + '" AND '
             '(severity>=ERROR OR '
             'textPayload=~"HUNTER_EXECUTION_FAILED|Traceback|Exception|RuntimeError|ValueError|WorkBudgetExceeded|MemoryError|Terminating task|Killed"))')
    raw = gcloud("logging", "read", query, "--limit=60", "--order=desc", "--format=json")
    rows = json.loads(raw)
    print("US_FAILURE_LOG_EXECUTION", execution, "MATCHES", len(rows), flush=True)
    for row in reversed(rows[-35:]):
        msg = row.get("textPayload") or (row.get("jsonPayload") or {}).get("message") or ""
        msg = re.sub(r"https?://\S+", "[URL]", str(msg))
        msg = re.sub(r"(?i)((?:password|token|secret|key)\s*[:=]\s*)\S+",
                     r"\1[REDACTED]", msg)
        if msg.strip():
            print("US_FAILURE_LOG", row.get("timestamp"), msg[:1800], flush=True)


def newest_failed_execution() -> str | None:
    raw = gcloud("run", "jobs", "executions", "list", "--job=" + JOB,
                 "--region=" + REGION, "--limit=5", "--format=json")
    for item in json.loads(raw):
        name = (item.get("metadata") or {}).get("name") or item.get("name") or ""
        name = name.rsplit("/", 1)[-1]
        status = item.get("status") or item
        completed = status.get("completionStatus") or item.get("completionStatus")
        if (re.fullmatch(r"hunter-us-daily-[a-z0-9]+", name)
                and completed in ("EXECUTION_FAILED", "CONDITION_FAILED")):
            return name
    return None


def bridge():
    def secret(name: str) -> str:
        return gcloud("secrets", "versions", "access", "latest", f"--secret={name}").strip()
    api = module(ROOT / "cloudrun/apps-script-post.py", "hunter_bridge_client")
    return api.post_json, secret("APPS_SCRIPT_WEBAPP_URL"), secret("APPS_SCRIPT_SHARED_KEY")


def read_document(api, url: str, key: str, path: str) -> dict:
    result = api(url, {"op": "read", "key": key, "path": path}, attempts=3)
    if result.get("ok") is not True or not result.get("data_base64"):
        raise RuntimeError("US_BRIDGE_AUTHORITY_UNREADABLE:" + path)
    raw = base64.b64decode(result["data_base64"], validate=True)
    return json.loads(raw)


def exists(api, url: str, key: str, path: str) -> bool:
    doc = api(url, {"op": "file", "key": key, "path": path}, attempts=3)
    if doc.get("ok") is not True:
        raise RuntimeError("US_BRIDGE_FILE_LOOKUP_FAILED:" + path)
    return bool(doc.get("file"))


def accepted(api, url: str, key: str) -> str:
    cp = read_document(api, url, key, "US/CONTROL/DAILY_CHECKPOINT.json")
    last = cp.get("last_completed_date")
    phase = cp.get("phase2_completed_date")
    if cp.get("market") != "US" or last not in (PREV, DAY, TARGET):
        raise RuntimeError("UNEXPECTED_US_CHECKPOINT:" + str(last))
    if last == TARGET:
        if phase != TARGET or not cp.get("phase2_completed_at_myt"):
            raise RuntimeError("US_TARGET_PHASE2_NOT_COMMITTED")
    # A validated rank/checkpoint can precede Phase 2 completion; the
    # existing auto worker resumes this exact committed day on its next pass.
    if last == DAY and phase not in (PREV, DAY):
        raise RuntimeError("US_UNEXPECTED_PHASE2_POINTER:" + str(phase))
    for date in (DAY, TARGET):
        if date > last:
            continue
        run = read_document(api, url, key, f"US/CONTROL/DAILY_RUN_{date}.json")
        if (run.get("market") != "US" or run.get("trade_date") != date
                or run.get("status") != "COMPLETE"):
            raise RuntimeError("US_RECEIPT_NOT_COMPLETE:" + date)
        if not exists(api, url, key, f"US/DERIVED/{date}/RANK.json"):
            raise RuntimeError("US_RANK_NOT_PUBLISHED:" + date)
    print("US_READBACK", json.dumps({"checkpoint": last, "phase2": phase},
                                     sort_keys=True), flush=True)
    return last


def do_execute(deploy) -> None:
    deploy.require_no_active_us_execution()
    # One explicitly requested manual execution of the EXISTING US worker.
    # No new schedule or parallel HK execution.
    try:
        shell("gcloud", "run", "jobs", "execute", JOB,
              f"--region={REGION}", f"--project={PROJECT}",
              "--wait", "--quiet", capture=False)
    except subprocess.CalledProcessError:
        # A failed Cloud Run operation is NOT a successful catchup. Read the
        # newest failed execution, print its actual error and stop immediately.
        try:
            failed = newest_failed_execution()
            if failed:
                log_failure(failed)
            else:
                print("US_EXECUTION_FAILED_LOG_ID_UNVERIFIED", flush=True)
        except Exception as diag_exc:
            print("US_EXECUTION_DIAGNOSTICS_UNAVAILABLE",
                  type(diag_exc).__name__, flush=True)
        raise


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--diagnose-only":
        log_failure(sys.argv[2])
        return  # Read-only: no auth secrets, deployment, lock change or job run.
    if len(sys.argv) != 1:
        raise RuntimeError("US_RECOVERY_ARGUMENTS_INVALID")
    if dt.datetime.now(MYT).date().isoformat() != "2026-10-25":
        raise RuntimeError("DATE_BOUND_ONE_OFF_ONLY_NO_LATE_RUN")
    if shell("git", "branch", "--show-current").strip() != "main":
        raise RuntimeError("PINNED_MAIN_REQUIRED")
    sha = shell("git", "rev-parse", "HEAD").strip()
    if shell("git", "status", "--porcelain").strip():
        raise RuntimeError("DIRTY_CHECKOUT_DENIED")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("BAD_HEAD_SHA")
    print("US_RECOVERY_SOURCE_SHA", sha, flush=True)
    deploy = module(ROOT / "cloudrun/deploy-us-phase2-fix-20261015.py",
                    "hunter_us_image_release")
    deploy.require_no_active_us_execution()
    api, url, key = bridge()
    initial = accepted(api, url, key)
    if initial == PREV and not exists(api, url, key, f"US/CONTROL/DAILY_RUN_{DAY}.json"):
        raise RuntimeError("REQUIRED_2026_10_22_COMPLETE_RECEIPT_MISSING")
    if initial == TARGET:
        print("US_TARGET_ALREADY_COMPLETED_NO_NEW_JOB")
        return
    log_failure()
    print("TESTING_BEFORE_DEPLOY", flush=True)
    shell(sys.executable, "-m", "unittest", "discover", "-s", "tests",
          "-p", "test_*.py", capture=False)
    print("DEPLOY_EXISTING_US_IMAGE_ONLY", flush=True)
    shell(sys.executable, "cloudrun/deploy-us-phase2-fix-20261015.py",
          sha, capture=False)

    # First pass closes 10/22 if already committed. Second pass catches 10/23.
    for pass_no in range(1, 3):
        before = accepted(api, url, key)
        if before == TARGET:
            print("US_CATCHUP_COMPLETE", TARGET, flush=True)
            return
        print("US_EXECUTE_PASS", pass_no, "FROM", before, flush=True)
        do_execute(deploy)
        after = accepted(api, url, key)
        if before == PREV and after != DAY:
            raise RuntimeError("US_2026_10_22_NOT_FULLY_COMMITTED_NO_SECOND_RUN")
        if before == DAY and after != TARGET:
            raise RuntimeError("US_2026_10_23_NOT_FULLY_COMMITTED")
        print("US_PASS_COMMITTED", after, flush=True)

    if accepted(api, url, key) != TARGET:
        raise RuntimeError("US_TARGET_NOT_REACHED")
    print("US_CATCHUP_COMPLETE", TARGET, flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("US_GUARDED_CATCHUP_STOPPED", type(exc).__name__, str(exc)[:400],
              file=sys.stderr, flush=True)
        sys.exit(2)
