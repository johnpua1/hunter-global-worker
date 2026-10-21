#!/usr/bin/env python3
"""Bind the existing HK DAILY job to an already deployed, pinned US image.

Image-only repair. No Cloud Run execution, scheduler, Apps Script trigger,
new resource, dataset write, or cross-market job update is performed.
Fail closed on unverifiable execution or any topology drift.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys

PROJECT = "rgs-hunter-global"
REGION = "us-central1"
IMAGE_BASE = f"{REGION}-docker.pkg.dev/{PROJECT}/hunter-worker/runner"
US_JOB = "hunter-us-daily"
HK_JOB = "hunter-hk-daily"


def gcloud(*args: str) -> str:
    return subprocess.check_output(
        ["gcloud", *args, "--project=" + PROJECT,
         "--region=" + REGION, "--format=json"], text=True
    )


def describe(job: str) -> dict:
    return json.loads(gcloud("run", "jobs", "describe", job))


def job_spec(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]


def task_spec(doc: dict) -> dict:
    return job_spec(doc)["template"]["spec"]


def only_container(doc: dict) -> dict:
    containers = task_spec(doc)["containers"]
    if len(containers) != 1:
        raise RuntimeError("HK_DEPLOY_CONTAINER_COUNT_DRIFT")
    return containers[0]


def source_env(container: dict) -> dict:
    values = {}
    for item in container.get("env", []):
        if item["name"] in values:
            raise RuntimeError("HK_DEPLOY_DUPLICATE_ENV:" + item["name"])
        values[item["name"]] = item
    return values


def verify_job(doc: dict, market: str) -> None:
    job = job_spec(doc)
    task = task_spec(doc)
    container = only_container(doc)
    if (container.get("args") != ["--mode", "auto", "--market", market]
            or container.get("command")
            or not container.get("image", "").startswith(IMAGE_BASE + ":")
            or job.get("taskCount") != 1
            or job.get("parallelism") != 1
            or int(task.get("timeoutSeconds", 0)) != 7200
            or task.get("maxRetries") != 0
            or not task.get("serviceAccountName", "").startswith(
                "hunter-" + market.lower() + "-daily@")):
        raise RuntimeError("HK_DEPLOY_EXISTING_TOPOLOGY_MISMATCH:" + market)


def normalized(doc: dict) -> dict:
    spec = copy.deepcopy(job_spec(doc))
    c = spec["template"]["spec"]["containers"][0]
    c["env"] = sorted(c.get("env", []), key=lambda item: item["name"])
    return spec


def expected(before: dict, image: str, sha: str) -> dict:
    spec = normalized(before)
    c = spec["template"]["spec"]["containers"][0]
    c["image"] = image
    env = source_env(c)
    env["HUNTER_SOURCE_SHA"] = {"name": "HUNTER_SOURCE_SHA", "value": sha}
    c["env"] = sorted(env.values(), key=lambda item: item["name"])
    return spec


# Both Cloud Run v1 ("status.conditions[].status" = True/False) and v2
# ("conditions[].state" = CONDITION_SUCCEEDED/FAILED) are in active use.
# The executions.list response may also lag a final executions.describe.
_TERMINAL = {"EXECUTION_SUCCEEDED", "EXECUTION_FAILED", "EXECUTION_CANCELLED"}
_NONTERMINAL = {"EXECUTION_PENDING", "EXECUTION_RUNNING"}


def execution_terminal(doc: dict) -> bool:
    """Only affirmative Cloud Run completion evidence qualifies as terminal."""
    if not isinstance(doc, dict):
        return False
    state = doc.get("status") if isinstance(doc.get("status"), dict) else doc
    for item in (doc, state):
        status = item.get("completionStatus")
        if status in _NONTERMINAL:
            return False  # Contradictory/stale state: demand fresh detail.
        if status in _TERMINAL:
            return True
    if state.get("completionTime") or doc.get("completionTime"):
        return True
    for condition in state.get("conditions") or []:
        if not isinstance(condition, dict) or condition.get("type") != "Completed":
            continue
        if condition.get("state") in ("CONDITION_SUCCEEDED", "CONDITION_FAILED"):
            return True
        # Cloud Run v1: a failed Completed condition is also terminal.
        if condition.get("status") in ("True", "False", True, False):
            return True
    return False


def execution_name(row: dict) -> str:
    name = row.get("name") or (row.get("metadata") or {}).get("name")
    if not isinstance(name, str) or not re.fullmatch(
            r"hunter-hk-daily-[a-z0-9-]+", name.rsplit("/", 1)[-1]):
        raise RuntimeError("HK_DEPLOY_EXECUTION_IDENTITY_UNVERIFIED")
    return name.rsplit("/", 1)[-1]


def require_no_active_hk_execution() -> None:
    rows = json.loads(gcloud(
        "run", "jobs", "executions", "list",
        "--job=" + HK_JOB, "--limit=100"
    ))
    if not isinstance(rows, list):
        raise RuntimeError("HK_DEPLOY_EXECUTION_STATUS_UNREADABLE")
    for row in rows:
        if execution_terminal(row):
            continue
        # A list entry without terminal proof must be checked individually.
        # Never assume a missing completion field proves that it is running.
        name = execution_name(row)
        detail = json.loads(gcloud(
            "run", "jobs", "executions", "describe", name
        ))
        if not execution_terminal(detail):
            raise RuntimeError(
                "HK_DEPLOY_EXECUTION_NOT_PROVEN_TERMINAL:" + name
            )


def main(sha: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise RuntimeError("HK_DEPLOY_PINNED_SHA_REQUIRED")
    image = IMAGE_BASE + ":" + sha
    us = describe(US_JOB)
    hk = describe(HK_JOB)
    verify_job(us, "US")
    verify_job(hk, "HK")
    if only_container(us)["image"] != image:
        raise RuntimeError("HK_DEPLOY_US_TESTED_IMAGE_NOT_DEPLOYED")
    env = source_env(only_container(us))
    if env.get("HUNTER_SOURCE_SHA", {}).get("value") != sha:
        raise RuntimeError("HK_DEPLOY_US_SOURCE_SHA_MISMATCH")
    current = normalized(hk)
    target = expected(hk, image, sha)
    if current == target:
        print("HK_IMAGE_ALREADY_CURRENT;NO_JOB_EXECUTION")
        return
    require_no_active_hk_execution()
    # Repeat all preconditions immediately before the image-only update.
    if normalized(describe(HK_JOB)) != current:
        raise RuntimeError("HK_DEPLOY_CONCURRENT_JOB_CHANGE")
    require_no_active_hk_execution()
    subprocess.run(
        ["gcloud", "run", "jobs", "update", HK_JOB,
         "--image=" + image, "--update-env-vars=HUNTER_SOURCE_SHA=" + sha,
         "--project=" + PROJECT, "--region=" + REGION, "--quiet"],
        check=True,
    )
    after = describe(HK_JOB)
    if normalized(after) != target:
        raise RuntimeError("HK_DEPLOY_IMAGE_ONLY_READBACK_MISMATCH")
    print("HK_EXISTING_IMAGE_UPDATED=" + sha)
    print("HK_ARGS_IAM_LIMITS_AND_SCHEDULER_UNCHANGED;NO_JOB_EXECUTION")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: rebind-hk-existing-image.py <PINNED_SHA>")
    main(sys.argv[1])
