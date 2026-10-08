"""Pinned cutover of existing US/HK daily jobs; never launch Phase 2."""
import copy
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

PROJECT = "rgs-hunter-global"
REGION = "us-central1"
IMAGE = "us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/daily-data:"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def gc(*args):
    cmd = ["gcloud", *args, "--project=" + PROJECT, "--quiet", "--format=json"]
    if args[0] == "run":
        cmd.append("--region=" + REGION)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode:
        # Do not print the job env/secret references or credential payloads.
        message = p.stderr.lower()
        category = "AUTH_REQUIRED" if any(s in message for s in ("reauth", "credential", "account", "login")) else "COMMAND_FAILED"
        raise RuntimeError(category + ":" + "_".join(args[:4]))
    return json.loads(p.stdout or "{}")


def container(doc):
    if "spec" in doc:
        containers = doc["spec"]["template"]["spec"]["template"]["spec"]["containers"]
    else:
        containers = doc["template"]["template"]["containers"]
    if len(containers) != 1:
        raise RuntimeError("EXPECTED_SINGLE_CONTAINER")
    return containers[0]


def active(row):
    status = row.get("status", row)
    terminal = any(c.get("type") == "Completed" and c.get("status") in ("True", "False", True, False)
                   for c in status.get("conditions", []))
    return not status.get("completionTime") and not terminal


def executions(market):
    return gc("run", "jobs", "executions", "list", "--job=hunter-" + market.lower() + "-daily", "--limit=100")


def name(row):
    return (row.get("metadata", {}).get("name") or row.get("name", "")).rsplit("/", 1)[-1]


def expected_container(old, market, sha):
    c = copy.deepcopy(old)
    c.pop("command", None)
    c.update(image=IMAGE + sha, args=["--mode", "daily-data", "--market", market])
    env = {e["name"]: e for e in c.get("env", [])}
    for k, v in {"HUNTER_SOURCE_SHA": sha, "HUNTER_PIPELINE": "DAILY_DATA_ONLY",
                 "HUNTER_PHASE2_ENABLED": "0", "HUNTER_CONTINUE_ONLY": "1",
                 "HUNTER_READ_CHECKPOINTS": "0", "HUNTER_INCREMENTAL_INPUTS": "0",
                 "HUNTER_RESUMABLE_RUN": "1"}.items():
        env[k] = {"name": k, "value": v}
    c["env"] = list(env.values())
    return c


def normalized(c):
    c = copy.deepcopy(c)
    if not c.get("command"):
        c.pop("command", None)
    c["env"] = sorted(c.get("env", []), key=lambda e: e["name"])
    return c


def deploy(sha):
    if not re.fullmatch("[0-9a-f]{40}", sha):
        raise RuntimeError("PINNED_SHA_REQUIRED")
    if subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip() != sha:
        raise RuntimeError("CHECKOUT_SHA_MISMATCH")
    before = {m: gc("run", "jobs", "describe", "hunter-" + m.lower() + "-daily") for m in ("US", "HK")}
    for market, doc in before.items():
        env = {e["name"]: e for e in container(doc).get("env", [])}
        if env.get("HUNTER_ACTIONS_CUTOVER", {}).get("value") != "CONFIRMED":
            raise RuntimeError("SINGLE_WRITER_CUTOVER_NOT_CONFIRMED:" + market)
        if not all(key in env for key in ("APPS_SCRIPT_WEBAPP_URL", "APPS_SCRIPT_SHARED_KEY")):
            raise RuntimeError("BRIDGE_CONFIGURATION_MISSING:" + market)
    desired = {m: expected_container(container(doc), m, sha) for m, doc in before.items()}
    needs_build = any(normalized(container(before[m])) != normalized(desired[m]) for m in before)
    if needs_build:
        with tempfile.TemporaryDirectory(prefix="daily-data-build-") as work:
            config = pathlib.Path(work) / "build.json"
            config.write_text(json.dumps({"steps": [{"name": "gcr.io/cloud-builders/docker",
                "args": ["build", "-f", "Dockerfile.daily-data", "--build-arg", "HUNTER_SOURCE_SHA=" + sha,
                         "-t", IMAGE + sha, "."]}], "images": [IMAGE + sha], "timeout": "1800s"}))
            print("BUILDING_DAILY_DATA_ONLY=" + sha, flush=True)
            subprocess.run(["gcloud", "builds", "submit", str(ROOT), "--config=" + str(config),
                            "--project=" + PROJECT, "--region=" + REGION, "--quiet"], check=True)
    # No rollback on failure; both templates must be confirmed before launch.
    for market in ("US", "HK"):
        job = "hunter-" + market.lower() + "-daily"
        current = gc("run", "jobs", "describe", job)
        if normalized(container(current)) != normalized(container(before[market])):
            raise RuntimeError("JOB_CHANGED_DURING_BUILD:" + market)
        if normalized(container(current)) != normalized(desired[market]):
            gc("run", "jobs", "update", job, "--image=" + IMAGE + sha, "--command=",
               "--args=--mode,daily-data,--market," + market, "--task-timeout=7200s", "--max-retries=0", "--tasks=1", "--parallelism=1",
               "--update-env-vars=HUNTER_SOURCE_SHA=" + sha + ",HUNTER_PIPELINE=DAILY_DATA_ONLY,HUNTER_PHASE2_ENABLED=0,HUNTER_CONTINUE_ONLY=1,HUNTER_READ_CHECKPOINTS=0,HUNTER_INCREMENTAL_INPUTS=0,HUNTER_RESUMABLE_RUN=1")
        updated = gc("run", "jobs", "describe", job)
        if normalized(container(updated)) != normalized(desired[market]):
            raise RuntimeError("DAILY_DATA_TEMPLATE_MISMATCH:" + market)
        print("DAILY_DATA_TEMPLATE_READY=" + market, flush=True)
    # Retiring Phase 2 also requires stopping its already-running daily workers.
    stopped = []
    for market in ("US", "HK"):
        for row in executions(market):
            if active(row):
                execution = name(row)
                if not re.fullmatch("hunter-" + market.lower() + r"-daily-[a-z0-9]+", execution):
                    raise RuntimeError("EXECUTION_NAME_INVALID")
                full = gc("run", "jobs", "executions", "describe", execution)
                # A same-version data-only worker launched by Scheduler can continue.
                cs = full.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                values = {e["name"]: e.get("value") for e in cs[0].get("env", [])} if cs else {}
                if (values.get("HUNTER_SOURCE_SHA") == sha
                        and values.get("HUNTER_PIPELINE") == "DAILY_DATA_ONLY"):
                    continue
                gc("run", "jobs", "executions", "cancel", execution)
                stopped.append(execution)
                print("RETIRED_WORKER_CANCEL_REQUESTED=" + execution, flush=True)
    deadline = time.monotonic() + 180
    while stopped:
        stopped = [n for n in stopped if active(gc("run", "jobs", "executions", "describe", n))]
        if not stopped:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("CANCEL_PENDING_NO_NEW_EXECUTION_STARTED")
        time.sleep(5)
    for market in ("US", "HK"):
        running = [r for r in executions(market) if active(r)]
        if running:
            print("DAILY_DATA_EXISTING_EXECUTION=" + name(running[0]), flush=True)
        else:
            result = gc("run", "jobs", "execute", "hunter-" + market.lower() + "-daily", "--async")
            print("DAILY_DATA_EXECUTION_REQUESTED=" + market, flush=True)
    print("DAILY_DATA_CUTOVER_DEPLOYED;PHASE2_REMOVED_FROM_DAILY_IMAGE;EXISTING_SCHEDULE_TARGETS_PRESERVED", flush=True)
    print("REAL_DAILY_SUCCESS_REQUIRES_DAILY_DATA_DONE_LOG;DEPLOYMENT_IS_NOT_DATA_ACCEPTANCE", flush=True)


if __name__ == "__main__":
    lock = open(pathlib.Path.home() / "hunter-phase2-closeout.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("STOP_OLD_PHASE2_MONITOR_WITH_CTRL_C_THEN_RUN_THIS_COMMAND;NO_CHANGES_MADE")
    try:
        deploy(sys.argv[1])
    finally:
        lock.close()
