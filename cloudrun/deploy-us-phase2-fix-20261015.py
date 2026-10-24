#!/usr/bin/env python3
"""Deploy an already-merged Hunter US Phase 2 code fix without launching jobs.

This changes only the existing hunter-us-daily image and HUNTER_SOURCE_SHA.
No DAILY, Phase 2, Cloud Scheduler, GitHub dispatch, or other market job runs.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "rgs-hunter-global"
REGION = "us-central1"
JOB = "hunter-us-daily"
REQUIRED_FIX = "3a0b5e0278dd624d5c430be6a09938d13b288a76"
IMAGE_ROOT = f"{REGION}-docker.pkg.dev/{PROJECT}/hunter-worker/runner"


def verify_checkout(sha: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise RuntimeError("PINNED_SHA_REQUIRED")
    head = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != sha:
        raise RuntimeError("PINNED_CHECKOUT_REQUIRED")
    subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", REQUIRED_FIX, sha],
        check=True, stdout=subprocess.DEVNULL,
    )


def read_job() -> dict:
    result = subprocess.check_output(
        ["gcloud", "run", "jobs", "describe", JOB, f"--project={PROJECT}",
         f"--region={REGION}", "--format=json"], text=True
    )
    return json.loads(result)


def task_spec(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]["template"]["spec"]


def job_spec(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]


def only_container(doc: dict) -> dict:
    containers = task_spec(doc)["containers"]
    if len(containers) != 1:
        raise RuntimeError("US_JOB_CONTAINER_COUNT_CHANGED")
    return containers[0]


def verify_existing_job(doc: dict) -> None:
    task = task_spec(doc)
    execution = job_spec(doc)
    container = only_container(doc)
    image = container.get("image", "")
    if (container.get("args") != ["--mode", "auto", "--market", "US"]
            or container.get("command")
            or not image.startswith(IMAGE_ROOT + ":")
            or int(task.get("timeoutSeconds", 0)) != 7200
            or task.get("maxRetries") != 0
            or execution.get("taskCount") != 1
            or execution.get("parallelism") != 1
            or not task.get("serviceAccountName", "").startswith("hunter-us-daily@")):
        raise RuntimeError("UNEXPECTED_US_JOB_CONFIGURATION")


def normalized_spec(doc: dict) -> dict:
    result = copy.deepcopy(job_spec(doc))
    container = result["template"]["spec"]["containers"][0]
    container["env"] = sorted(container.get("env", []), key=lambda entry: entry["name"])
    return result


def expected_spec(before: dict, image: str, sha: str) -> dict:
    spec = normalized_spec(before)
    container = spec["template"]["spec"]["containers"][0]
    container["image"] = image
    env = {entry["name"]: entry for entry in container.get("env", [])}
    env["HUNTER_SOURCE_SHA"] = {"name": "HUNTER_SOURCE_SHA", "value": sha}
    container["env"] = sorted(env.values(), key=lambda entry: entry["name"])
    return spec


def build(sha: str) -> None:
    image = IMAGE_ROOT + ":" + sha
    config = {"steps": [{"name": "gcr.io/cloud-builders/docker",
                          "args": ["build", "--build-arg", "HUNTER_SOURCE_SHA=" + sha,
                                   "-t", image, "."]}],
              "images": [image], "timeout": "1800s"}
    with tempfile.TemporaryDirectory(prefix="hunter-us-phase2-deploy-") as folder:
        config_path = Path(folder) / "build.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        subprocess.run(
            ["gcloud", "builds", "submit", str(ROOT), f"--config={config_path}",
             f"--project={PROJECT}", f"--region={REGION}", "--quiet"], check=True
        )


def update_job(image: str, sha: str) -> None:
    subprocess.run(
        ["gcloud", "run", "jobs", "update", JOB, "--image=" + image,
         "--update-env-vars=HUNTER_SOURCE_SHA=" + sha,
         f"--project={PROJECT}", f"--region={REGION}", "--quiet"], check=True
    )


# Keep deployment fail-closed while either DAILY job has an unproven
# terminal execution. The image update affects later runs; do not race a
# producer that may still be committing DAILY / RANK / Phase 2 receipts.
def _terminal_execution(doc: dict) -> bool:
    if not isinstance(doc, dict):
        return False
    status = doc.get("status") if isinstance(doc.get("status"), dict) else doc
    for state in (doc, status):
        completion = state.get("completionStatus")
        if completion in ("EXECUTION_PENDING", "EXECUTION_RUNNING"):
            return False
        if completion in ("EXECUTION_SUCCEEDED", "EXECUTION_FAILED",
                          "EXECUTION_CANCELLED"):
            return True
    if status.get("completionTime") or doc.get("completionTime"):
        return True
    for condition in status.get("conditions") or []:
        if condition.get("type") != "Completed":
            continue
        if condition.get("state") in ("CONDITION_SUCCEEDED", "CONDITION_FAILED"):
            return True
        if condition.get("status") in ("True", "False", True, False):
            return True
    return False


def require_no_active_us_execution() -> None:
    rows = json.loads(subprocess.check_output(
        ["gcloud", "run", "jobs", "executions", "list",
         "--job=" + JOB, "--limit=100", f"--project={PROJECT}",
         f"--region={REGION}", "--format=json"], text=True))
    if not isinstance(rows, list):
        raise RuntimeError("US_DEPLOY_EXECUTION_STATUS_UNREADABLE")
    for row in rows:
        if _terminal_execution(row):
            continue
        name = row.get("name") or (row.get("metadata") or {}).get("name")
        if not isinstance(name, str) or not re.fullmatch(
                r"hunter-us-daily-[a-z0-9-]+", name.rsplit("/", 1)[-1]):
            raise RuntimeError("US_DEPLOY_EXECUTION_IDENTITY_UNVERIFIED")
        name = name.rsplit("/", 1)[-1]
        detail = json.loads(subprocess.check_output(
            ["gcloud", "run", "jobs", "executions", "describe", name,
             f"--project={PROJECT}", f"--region={REGION}",
             "--format=json"], text=True))
        if not _terminal_execution(detail):
            raise RuntimeError("US_DEPLOY_EXECUTION_NOT_PROVEN_TERMINAL:" + name)


def main(sha: str) -> None:
    os.umask(0o077)
    verify_checkout(sha)
    image = IMAGE_ROOT + ":" + sha
    before = read_job()
    verify_existing_job(before)
    require_no_active_us_execution()
    if normalized_spec(before) == expected_spec(before, image, sha):
        print("US_PHASE2_IMAGE_ALREADY_VERIFIED;NO_EXECUTION_STARTED")
        return
    if only_container(before)["image"] != image:
        build(sha)
    current = read_job()
    if normalized_spec(current) != normalized_spec(before):
        raise RuntimeError("US_JOB_CHANGED_DURING_BUILD")
    require_no_active_us_execution()
    update_job(image, sha)
    after = read_job()
    if normalized_spec(after) != expected_spec(before, image, sha):
        raise RuntimeError("US_DEPLOY_READBACK_MISMATCH")
    print("US_PHASE2_IMAGE_DEPLOYED_AND_VERIFIED=" + sha)
    print("OTHER_JOBS_UNCHANGED;NO_EXECUTION_STARTED;NEXT_SCHEDULED_RUN_USES_FIX")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python cloudrun/deploy-us-phase2-fix-20261015.py <PINNED_MAIN_SHA>")
    main(sys.argv[1])
