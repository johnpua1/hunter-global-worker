#!/usr/bin/env python3
"""Build the reviewed repair fix, wait for idle, enroll MAINT only and resume once."""
import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BASE = "19a421ba4fe0fb6f43ced2dd1a0490bf3ecfdc0b"
RUNTIME_RELEASE = "cf508f3ab4ecc5db3bc429cff51ebb45af944898"
PROJECT = "rgs-hunter-global"
REGION = "us-central1"
JOB = "hunter-maintenance"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / "cloudrun" / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def stamp(message):
    print(dt.datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).strftime("%H:%M:%S MYT ") + message, flush=True)


def wait_idle(deploy, allow_wait, timeout_seconds=7200):
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            deploy.ensure_idle()
            stamp("ALL_JOBS_IDLE")
            return
        except RuntimeError as exc:
            if not str(exc).startswith("JOB_STILL_RUNNING:"):
                raise
            stamp(str(exc))
            if not allow_wait:
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError("IDLE_WAIT_LIMIT_REACHED_NO_DEPLOY") from exc
            # Show the latest Maintenance stage on every bounded check.
            query = ('resource.type="cloud_run_job" AND resource.labels.job_name="hunter-maintenance" '
                     'AND textPayload:"maintenance stage="')
            logs = deploy.command(["gcloud", "logging", "read", query, "--project=" + PROJECT,
                                   "--freshness=3h", "--order=desc", "--limit=1", "--format=json"])
            for entry in json.loads(logs):
                message = entry.get("textPayload", "")
                offset = message.find("maintenance stage=")
                if offset >= 0:
                    stamp("LAST_LOG " + message[offset:])
            time.sleep(60)


def ensure_image(image):
    result = subprocess.run(["gcloud", "artifacts", "docker", "images", "describe", image,
                             "--project=" + PROJECT, "--format=json"], text=True, capture_output=True)
    if result.returncode == 0:
        stamp("BUILT_IMAGE_EXISTS")
        return
    if not re.search(r"NOT_FOUND|not found|does not exist", result.stderr):
        raise RuntimeError("IMAGE_LOOKUP_FAILED")
    stamp("BUILD_MAINTENANCE_FIX")
    subprocess.run(["gcloud", "builds", "submit", str(ROOT), "--tag=" + image,
                    "--project=" + PROJECT, "--region=" + REGION, "--quiet"], check=True)


def reviewed_release(deploy):
    head = deploy.command(["git", "rev-parse", "HEAD"], ROOT).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError("RELEASE_SHA_INVALID")
    # Reuse the already-built image only when runtime and Dockerfile are exact.
    delta = deploy.command(["git", "diff", "--name-only", RUNTIME_RELEASE, head,
                            "--", "hunter-global", "Dockerfile"], ROOT)
    if delta.strip():
        raise RuntimeError("RUNTIME_DIFFERS_FROM_BUILT_REPAIR_IMAGE")
    changed = deploy.command(["git", "diff", "--name-only", BASE, RUNTIME_RELEASE,
                              "--", "hunter-global", "Dockerfile"], ROOT)
    if set(changed.splitlines()) != {"hunter-global/repair.py"}:
        raise RuntimeError("UNREVIEWED_RUNTIME_CHANGE")
    if deploy.command(["git", "status", "--porcelain", "--untracked-files=no"], ROOT).strip():
        raise RuntimeError("DIRTY_RELEASE_CHECKOUT")
    return RUNTIME_RELEASE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-idle", action="store_true", help="Check every 60 seconds for up to 2 hours")
    options = parser.parse_args()
    deploy = module("scoped_script_deploy", "deploy-monthly-retry.py")
    guard = module("guard_enrollment", "enroll-production-guard.py")
    release = reviewed_release(deploy)
    guard.PREVIOUS[JOB] = (BASE, "MAINT", "2")
    before = guard.gc("run", "jobs", "describe", JOB)
    guard.validate_previous(JOB, before, release)
    image = REGION + "-docker.pkg.dev/" + PROJECT + "/hunter-worker/runner:" + release
    already = guard.parts(before)[2]["image"] == image
    if not already:
        # Building doesn't change the running execution or deployed job.
        ensure_image(image)
    wait_idle(deploy, options.wait_idle)
    # Finish the already-tested Apps Script launch guard while every job is idle.
    deploy.main(launch_guard=True)
    wait_idle(deploy, options.wait_idle)
    if already:
        guard.probe(JOB, "VERIFY")
        stamp("MAINTENANCE_PATCH_ALREADY_DEPLOYED_AND_GUARD_VERIFIED_NO_EXTRA_RUN")
        return
    guard.main(release=release, jobs=[JOB])
    stamp("MAINTENANCE_PATCH_DEPLOYED_AND_GUARD_VERIFIED")
    # Enrollment probes are terminal; start one real repair execution.
    wait_idle(deploy, options.wait_idle)
    stamp("START_MAINTENANCE_REPAIR_ONCE")
    result = guard.gc("run", "jobs", "execute", JOB)
    name = result.get("metadata", {}).get("name") or result.get("name")
    if not name:
        raise RuntimeError("LAUNCH_RESPONSE_WITHOUT_EXECUTION_NAME_DO_NOT_RETRY_BLINDLY")
    stamp("REPAIR_EXECUTION_STARTED=" + name)
    stamp("DEPLOY_COMPLETE_REPAIR_RESULT_STILL_PENDING")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc))
