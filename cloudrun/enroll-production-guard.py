#!/usr/bin/env python3
"""Deploy a built release, enroll read-only probes, and verify all four guards.

Run from the checked-out release root. No key payloads are read or printed.
Only the failing job is rolled back; already verified jobs remain protected.
"""
import json
import argparse
import datetime as dt
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

PROJECT = "rgs-hunter-global"
REGION = "us-central1"
PREVIOUS = {
    "hunter-us-daily": ("4a6dc044574ec3a9097f1e5ae22cf53f197a886f", "US", "2"),
    "hunter-hk-daily": ("79517d4e89dd6f9cc1680cef4868efe5d5a7537d", "HK", "2"),
    "hunter-maintenance": ("78001fc48c579688261b1042b675465a480c4b8f", "MAINT", "2"),
    "hunter-monthly-v2": ("19979eeb812e86c042d0f1d3b71fe8280aeabc9f", "MONTH", "1"),
}


def gc(*args, region=True):
    command = ["gcloud", *args, "--project=" + PROJECT, "--format=json", "--quiet"]
    if region:
        command.append("--region=" + REGION)
    output = subprocess.check_output(command, text=True)
    return json.loads(output) if output.strip() else {}


def parts(doc):
    execution = doc["spec"]["template"]["spec"]
    task = execution["template"]["spec"]
    if len(task["containers"]) != 1:
        raise RuntimeError("MULTIPLE_CONTAINERS")
    container = task["containers"][0]
    env = {x["name"]: x for x in container.get("env", [])}
    return execution, task, container, env


def validate_previous(job, doc, release):
    execution, task, c, env = parts(doc)
    old_sha, scope, version = PREVIOUS[job]
    image_root = REGION + "-docker.pkg.dev/" + PROJECT + "/hunter-worker/runner:"
    if c["image"] not in {image_root + old_sha, image_root + release}:
        raise RuntimeError("UNEXPECTED_IMAGE:" + job)
    if env.get("HUNTER_SOURCE_SHA", {}).get("value") != c["image"].rsplit(":", 1)[-1]:
        raise RuntimeError("SOURCE_IMAGE_MISMATCH:" + job)
    if execution.get("taskCount") != 1 or execution.get("parallelism") != 1:
        raise RuntimeError("TASK_TOPOLOGY_MISMATCH:" + job)
    account = "hunter-monthly" if scope == "MONTH" else job
    if task.get("serviceAccountName") != account + "@" + PROJECT + ".iam.gserviceaccount.com":
        raise RuntimeError("SERVICE_ACCOUNT_MISMATCH:" + job)
    expected_args = (["/app/maintenance.py"] if scope == "MAINT" else
                     ["--mode", "monthly"] if scope == "MONTH" else
                     ["--mode", "auto", "--market", scope])
    expected_command = ["python"] if scope == "MAINT" else []
    if c.get("args", []) != expected_args or c.get("command", []) != expected_command:
        raise RuntimeError("ENTRYPOINT_MISMATCH:" + job)
    ref = env.get("APPS_SCRIPT_SHARED_KEY", {}).get("valueFrom", {}).get("secretKeyRef", {})
    if ref != {"name": "APPS_SCRIPT_SHARED_KEY_" + scope, "key": version}:
        raise RuntimeError("SECRET_REFERENCE_MISMATCH:" + job)
    if "HUNTER_CONFIG_PROBE" in env:
        raise RuntimeError("PERSISTENT_PROBE_FORBIDDEN:" + job)


def show_probe_diagnostics(job, since):
    query = ('resource.type="cloud_run_job" AND resource.labels.job_name="' + job +
             '" AND timestamp>="' + since +
             '" AND textPayload=~"HUNTER_CONFIG_(DETAIL|BLOCKED) worker="')
    try:
        rows = gc("logging", "read", query, "--limit=20", "--order=asc", region=False)
        for row in rows:
            print("PROBE_DIAGNOSTIC", row.get("textPayload", ""), flush=True)
    except Exception:
        print("PROBE_DIAGNOSTIC_READ_FAILED", job, flush=True)


def prepare_config_reader(jobs):
    role_id = "hunterExecutionConfigReader"
    role_name = "projects/" + PROJECT + "/roles/" + role_id
    roles = gc("iam", "roles", "list", "--filter=name=" + role_name, region=False)
    if not roles:
        role = gc("iam", "roles", "create", role_id,
                  "--title=Hunter execution config reader", "--stage=GA",
                  "--permissions=run.executions.get", region=False)
    elif len(roles) == 1:
        role = gc("iam", "roles", "describe", role_id, region=False)
    else:
        raise RuntimeError("CONFIG_READER_ROLE_AMBIGUOUS")
    if role.get("deleted") or set(role.get("includedPermissions", [])) != {"run.executions.get"}:
        raise RuntimeError("CONFIG_READER_ROLE_PERMISSION_MISMATCH")
    for job in jobs:
        account = "hunter-monthly" if job == "hunter-monthly-v2" else job
        gc("run", "jobs", "add-iam-policy-binding", job,
           "--member=serviceAccount:" + account + "@" + PROJECT + ".iam.gserviceaccount.com",
           "--role=" + role_name, "--condition=None")
    print("OWN_JOB_CONFIG_READ_GRANTED", flush=True)


def probe(job, mode):
    print("READ_ONLY_PROBE", job, mode, flush=True)
    since = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        execution = gc("run", "jobs", "execute", job,
                       "--update-env-vars=HUNTER_CONFIG_PROBE=" + mode,
                       "--task-timeout=120s", "--wait")
    except Exception:
        show_probe_diagnostics(job, since)
        raise
    name = execution.get("metadata", {}).get("name") or execution.get("name", "").rsplit("/", 1)[-1]
    if not name.startswith(job + "-"):
        raise RuntimeError("EXECUTION_ID_MISSING:" + job)
    marker = "HUNTER_CONFIG_ENROLL" if mode == "ENROLL" else "HUNTER_CONFIG_OK"
    query = ('resource.type="cloud_run_job" AND resource.labels.job_name="' + job +
             '" AND labels."run.googleapis.com/execution_name"="' + name +
             '" AND textPayload:"' + marker + '"')
    for attempt in range(12):
        logs = gc("logging", "read", query, "--freshness=30m", "--limit=20", region=False)
        hashes = set()
        for row in logs:
            text = row.get("textPayload", "")
            match = re.search(marker + r" worker=" + re.escape(job) +
                              r" .*?sha256=([0-9a-f]{64})\b", text)
            if match:
                hashes.add(match[1])
        if len(hashes) == 1:
            # An eventual success after a configuration block is not a clean
            # probe. Cloud Run retries must not hide an unstable fingerprint.
            blocked_query = query.replace(marker, "HUNTER_CONFIG_BLOCKED")
            blocked = gc("logging", "read", blocked_query, "--freshness=30m", "--limit=20", region=False)
            if blocked:
                show_probe_diagnostics(job, since)
                raise RuntimeError("PROBE_RETRIED_AFTER_CONFIG_BLOCK:" + job)
            return hashes.pop()
        if hashes:
            raise RuntimeError("INCONSISTENT_PROBE_HASH:" + job)
        if attempt < 11:
            time.sleep(5)
    raise RuntimeError("PROBE_LOG_NOT_VISIBLE:" + job)


def main(release=None, jobs=None, prepare_iam=False):
    os.umask(0o077)
    release = release or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise RuntimeError("RELEASE_SHA_INVALID")
    selected = list(PREVIOUS) if jobs is None else list(jobs)
    if not selected or len(set(selected)) != len(selected) or any(j not in PREVIOUS for j in selected):
        raise RuntimeError("JOB_SELECTION_INVALID")
    backup = Path(tempfile.mkdtemp(prefix="hunter-guard-backup."))
    print("BACKUP", backup, flush=True)
    docs = {}
    for job in selected:
        doc = gc("run", "jobs", "describe", job)
        (backup / (job + ".json")).write_text(json.dumps(doc))
        validate_previous(job, doc, release)
        docs[job] = doc
    if prepare_iam:
        prepare_config_reader(selected)
        return
    image = REGION + "-docker.pkg.dev/" + PROJECT + "/hunter-worker/runner:" + release
    for job, before in docs.items():
        _, _, old_container, old_env = parts(before)
        print("GUARD_DEPLOY", job, flush=True)
        try:
            gc("run", "jobs", "update", job, "--image=" + image,
               "--update-env-vars=HUNTER_SOURCE_SHA=" + release)
            actual = probe(job, "ENROLL")
            gc("run", "jobs", "update", job, "--update-env-vars=HUNTER_CONFIG_SHA256=" + actual)
            verified = probe(job, "VERIFY")
            if actual != verified:
                raise RuntimeError("PROBE_HASH_CHANGED:" + job)
            after = gc("run", "jobs", "describe", job)
            validate_previous(job, after, release)
            if (parts(after)[2]["image"] != image or
                    parts(after)[3].get("HUNTER_CONFIG_SHA256", {}).get("value") != actual):
                raise RuntimeError("GUARD_READBACK_MISMATCH:" + job)
            print("GUARD_VERIFIED", job, flush=True)
        except Exception:
            print("GUARD_FAILED_ROLLING_BACK", job, flush=True)
            old_sha = old_env["HUNTER_SOURCE_SHA"]["value"]
            old_hash = old_env.get("HUNTER_CONFIG_SHA256", {}).get("value")
            env = "HUNTER_SOURCE_SHA=" + old_sha
            extra = []
            if old_hash:
                env += ",HUNTER_CONFIG_SHA256=" + old_hash
            else:
                extra.append("--remove-env-vars=HUNTER_CONFIG_SHA256")
            gc("run", "jobs", "update", job, "--image=" + old_container["image"],
               "--update-env-vars=" + env, *extra)
            raise
    if len(selected) == 4:
        print("FOUR_JOB_GUARD_VERIFIED", flush=True)
    else:
        print("SELECTED_JOB_GUARDS_VERIFIED", ",".join(selected), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", help="SHA of an already-built image; defaults to this checkout")
    parser.add_argument("--jobs", nargs="+", choices=list(PREVIOUS))
    parser.add_argument("--prepare-iam", action="store_true", help="Grant own-job execution config read only; do not deploy")
    options = parser.parse_args()
    main(release=options.release, jobs=options.jobs, prepare_iam=options.prepare_iam)
