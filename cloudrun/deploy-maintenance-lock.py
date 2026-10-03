#!/usr/bin/env python3
"""Wait for current executions, deploy only MAINT mutex, verify, resume once."""
import argparse
import importlib.util
import re
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = "cf508f3ab4ecc5db3bc429cff51ebb45af944898"
JOB = "hunter-maintenance"


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "cloudrun" / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def reviewed_release(deploy):
    release = deploy.command(["git", "rev-parse", "HEAD"], ROOT).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise RuntimeError("RELEASE_SHA_INVALID")
    changed = deploy.command(["git", "diff", "--name-only", BASE, release,
                              "--", "hunter-global", "Dockerfile"], ROOT)
    if set(changed.splitlines()) != {"hunter-global/maintenance.py", "hunter-global/maintenance_lock.py"}:
        raise RuntimeError("UNREVIEWED_MUTEX_RUNTIME_CHANGE")
    if deploy.command(["git", "status", "--porcelain", "--untracked-files=no"], ROOT).strip():
        raise RuntimeError("DIRTY_RELEASE_CHECKOUT")
    return release


def verify_lock_probe(guard):
    result = guard.gc("run", "jobs", "execute", JOB,
                      "--update-env-vars=HUNTER_MAINTENANCE_LOCK_PROBE=VERIFY",
                      "--task-timeout=180s", "--wait")
    name = result.get("metadata", {}).get("name") or result.get("name", "").rsplit("/", 1)[-1]
    if not re.fullmatch(r"hunter-maintenance-[a-z0-9]+", name):
        raise RuntimeError("LOCK_PROBE_EXECUTION_UNCONFIRMED")
    query = ('resource.type="cloud_run_job" AND resource.labels.job_name="hunter-maintenance" AND '
             'labels."run.googleapis.com/execution_name"="' + name +
             '" AND textPayload:"MAINTENANCE_LOCK_VERIFIED execution=' + name + '"')
    for attempt in range(12):
        logs = guard.gc("logging", "read", query, "--freshness=30m", "--limit=5", region=False)
        if any("MAINTENANCE_LOCK_VERIFIED execution=" + name in x.get("textPayload", "") for x in logs):
            return
        if attempt < 11:
            time.sleep(5)
    raise RuntimeError("LOCK_PROBE_MARKER_NOT_VISIBLE")


def rollback(guard, before):
    _, _, container, env = guard.parts(before)
    guard.gc("run", "jobs", "update", JOB, "--image=" + container["image"],
             "--update-env-vars=HUNTER_SOURCE_SHA=" + env["HUNTER_SOURCE_SHA"]["value"] +
             ",HUNTER_CONFIG_SHA256=" + env["HUNTER_CONFIG_SHA256"]["value"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-idle", action="store_true")
    options = parser.parse_args()
    deploy = module("mutex_script_helpers", "deploy-monthly-retry.py")
    repair = module("mutex_repair_helpers", "deploy-maintenance-repair.py")
    guard = module("mutex_guard_helpers", "enroll-production-guard.py")
    release = reviewed_release(deploy)
    guard.PREVIOUS[JOB] = (BASE, "MAINT", "2")
    before = guard.gc("run", "jobs", "describe", JOB)
    guard.validate_previous(JOB, before, release)
    if "HUNTER_MAINTENANCE_LOCK_PROBE" in guard.parts(before)[3]:
        raise RuntimeError("PERSISTENT_LOCK_PROBE_FORBIDDEN")
    image = guard.REGION + "-docker.pkg.dev/" + guard.PROJECT + "/hunter-worker/runner:" + release
    already = guard.parts(before)[2]["image"] == image
    if not already:
        repair.ensure_image(image)
    repair.wait_idle(deploy, options.wait_idle)
    if already:
        guard.probe(JOB, "VERIFY")
    else:
        guard.main(release=release, jobs=[JOB])
    repair.wait_idle(deploy, options.wait_idle)
    try:
        verify_lock_probe(guard)
    except Exception:
        if not already:
            repair.stamp("LOCK_PROBE_FAILED_ROLLING_BACK")
            rollback(guard, before)
        raise
    repair.stamp("MAINTENANCE_MUTEX_DEPLOYED_AND_VERIFIED")
    if already:
        repair.stamp("ALREADY_DEPLOYED_NO_EXTRA_REPAIR_RUN")
        return
    repair.wait_idle(deploy, options.wait_idle)
    result = guard.gc("run", "jobs", "execute", JOB)
    name = result.get("metadata", {}).get("name") or result.get("name", "").rsplit("/", 1)[-1]
    if not re.fullmatch(r"hunter-maintenance-[a-z0-9]+", name):
        raise RuntimeError("LAUNCH_RESPONSE_UNCONFIRMED_DO_NOT_RETRY_BLINDLY")
    repair.stamp("REPAIR_EXECUTION_STARTED=" + name)
    repair.stamp("DEPLOY_COMPLETE_REPAIR_RESULT_STILL_PENDING")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc))
