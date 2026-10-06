#!/usr/bin/env python3
"""Scoped HK timeout mitigation using the already-deployed, tested image.

Run in authenticated Cloud Shell. No credentials are read or printed. This is
a recovery attempt, not proof that DAILY or derived data have caught up.
"""
import json
import re
import subprocess
import time

PROJECT = "rgs-hunter-global"
REGION = "us-central1"
JOB = "hunter-hk-daily"
OLD = "hunter-hk-daily-b255z"
IMAGE = ("us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:"
         "859323f28a76057b51c5e807feec2db6a9aa3b5f")
SETTINGS = {"DAILY_READ_WORKERS": "2", "DERIVED_READ_WORKERS": "2",
            "HUNTER_BRIDGE_READ_TIMEOUT_SECONDS": "90"}


def gc(*args):
    command = ["gcloud", *args, "--project=" + PROJECT,
               "--quiet", "--format=json"]
    if args[0] == "run":
        command.append("--region=" + REGION)
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        # gcloud stderr can contain request URLs. Keep failures credential-free.
        raise RuntimeError("GCLOUD_FAILED:" + " ".join(args[:3]))
    return json.loads(result.stdout or "{}")


def name(doc):
    return (doc.get("metadata", {}).get("name") or doc.get("name", "")).rsplit("/", 1)[-1]


def state(doc):
    status = doc.get("status", doc)
    completed = next((c for c in status.get("conditions", [])
                      if c.get("type") == "Completed"), {})
    if completed.get("status") == "True" or completed.get("state") == "CONDITION_SUCCEEDED":
        return "SUCCEEDED"
    if completed.get("status") == "False" or completed.get("state") == "CONDITION_FAILED":
        return "FAILED"
    if status.get("completionTime"):
        return "TERMINAL"
    return "ACTIVE"  # Includes pending/retrying; runningCount=0 is not idle.


def containers(doc):
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key == "containers":
                yield from value
            else:
                yield from containers(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from containers(value)


def validate_job(doc, tuned=False):
    rows = list(containers(doc))
    if len(rows) != 1 or rows[0].get("image") != IMAGE:
        raise RuntimeError("EXPECTED_DEPLOYED_IMAGE_REQUIRED")
    if rows[0].get("args") != ["--mode", "auto", "--market", "HK"]:
        raise RuntimeError("HK_ARGS_CHANGED")
    env = {v["name"]: v.get("value") for v in rows[0].get("env", [])}
    if tuned and any(env.get(k) != v for k, v in SETTINGS.items()):
        raise RuntimeError("SETTINGS_READBACK_FAILED")


def executions():
    rows = gc("run", "jobs", "executions", "list", "--job=" + JOB,
              "--sort-by=~metadata.creationTimestamp")
    if not isinstance(rows, list) or any(not name(row) for row in rows):
        raise RuntimeError("EXECUTION_LIST_INVALID")
    return rows


def cancel_old():
    print("STOP_OLD=" + OLD, flush=True)
    try:
        gc("run", "jobs", "executions", "cancel", OLD, "--async")
    except RuntimeError:
        if state(gc("run", "jobs", "executions", "describe", OLD)) == "ACTIVE":
            raise
    for attempt in range(13):
        if state(gc("run", "jobs", "executions", "describe", OLD)) != "ACTIVE":
            return
        if attempt < 12:
            time.sleep(5)
    raise RuntimeError("CANCEL_NOT_CONFIRMED_NO_REPLACEMENT_STARTED")


def main():
    validate_job(gc("run", "jobs", "describe", JOB))
    gc("run", "jobs", "update", JOB, "--update-env-vars=" +
       ",".join(k + "=" + v for k, v in SETTINGS.items()))
    validate_job(gc("run", "jobs", "describe", JOB), tuned=True)
    print("HK_SETTINGS_VERIFIED=read_timeout_90s,read_workers_2", flush=True)
    rows = executions()
    if any(name(row) == OLD and state(row) == "ACTIVE" for row in rows):
        cancel_old()
    rows = executions()  # Fresh check after cancellation; respect other launchers.
    active = [row for row in rows if state(row) == "ACTIVE"]
    if active:
        print("EXISTING_EXECUTIONS=" + ",".join(name(row) for row in active))
        print("NO_ADDITIONAL_EXECUTION_STARTED")
    elif rows and state(rows[0]) == "SUCCEEDED":
        print("LATEST_EXECUTION_ALREADY_SUCCEEDED_NO_RESTART")
    else:
        launched = gc("run", "jobs", "execute", JOB, "--async")
        own = name(launched)
        if not own.startswith(JOB + "-"):
            raise RuntimeError("LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT_BLINDLY")
        print("STARTED=" + own, flush=True)
        # A direct Cloud Shell launch cannot share Apps Script's ScriptLock.
        # Detect an observed race and cancel only this script's own new launch.
        active = [row for row in executions() if state(row) == "ACTIVE"]
        if any(name(row) != own for row in active):
            gc("run", "jobs", "executions", "cancel", own, "--async")
            print("CONCURRENT_LAUNCH_DETECTED_OWN_EXECUTION_CANCEL_REQUESTED")
    print("DATA_RECOVERY=NOT_YET_VERIFIED")
    print("CONFIG_DRIFT_WARNING=NOT_REENROLLED")
    print("US_EXECUTION=UNCHANGED")
    for job in ("hunter-us-daily", JOB):
        query = ('resource.type="cloud_run_job" AND resource.labels.job_name="'
                 + job + '" AND timestamp>="2026-10-06T14:38:00Z"')
        rows = gc("logging", "read", query, "--limit=3", "--order=desc")
        for row in rows:
            message = row.get("textPayload") or row.get("jsonPayload", {}).get("message", "")
            message = re.sub(r"https?://\S+", "[URL]", str(message))
            if message.strip():
                print(job + " " + row.get("timestamp", "") + " " + message.splitlines()[-1][:240])


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        print("RECOVERY_STOPPED=" + str(exc), flush=True)
        raise SystemExit(1)
