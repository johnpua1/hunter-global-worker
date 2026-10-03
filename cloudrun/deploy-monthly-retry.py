#!/usr/bin/env python3
"""Patch only monthlyV2 in the existing Apps Script deployment, with rollback."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

PROJECT = "rgs-hunter-global"
REGION = "us-central1"
BASE = "19a421ba4fe0fb6f43ced2dd1a0490bf3ecfdc0b"
ROOT = Path(__file__).resolve().parents[1]
CLASP = ["npx", "-y", "@google/clasp@3.4.1"]
JOBS = ("hunter-us-daily", "hunter-hk-daily", "hunter-maintenance", "hunter-monthly-v2")


def command(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        # Avoid echoing tool output that could include credentials or project source.
        raise RuntimeError("COMMAND_FAILED:" + " ".join(argv[:4]))
    return result.stdout


def clasp(*args, cwd):
    return json.loads(command(CLASP + list(args) + ["--json"], cwd))


def secret(name):
    encoded = command(["gcloud", "secrets", "versions", "access", "latest",
                       "--secret=" + name, "--project=" + PROJECT,
                       "--format=get(payload.data)"]).strip()
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()


def source_files(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix not in (".gs", ".js", ".html", ".json"):
            continue
        relative = path.relative_to(directory)
        key = str(relative.with_suffix(".gs")) if path.suffix == ".js" else str(relative)
        if key in result:
            raise RuntimeError("DUPLICATE_SCRIPT_FILE")
        value = path.read_text()
        result[key] = json.loads(value) if path.suffix == ".json" else value
    return result


def monthly_function(source):
    matches = list(re.finditer(r"^function monthlyV2\(\) \{.*?\n\}\n(?=\nfunction listMonthlyTrigger\()",
                              source, re.M | re.S))
    if len(matches) != 1:
        raise RuntimeError("MONTHLY_FUNCTION_NOT_UNIQUE_OR_CHANGED")
    return matches[0].group()


def patch_monthly(directory, old, new):
    candidates = []
    for path in directory.rglob("*"):
        if path.suffix in (".gs", ".js") and path.is_file():
            content = path.read_text()
            if re.search(r"function\s+monthlyV2\s*\(", content):
                candidates.append((path, content))
    if len(candidates) != 1:
        raise RuntimeError("MONTHLY_HANDLER_NOT_UNIQUE")
    path, content = candidates[0]
    actual = monthly_function(content)
    if actual == new:
        return False
    if actual != old:
        raise RuntimeError("LIVE_MONTHLY_DIFFERS_FROM_REVIEWED_BASE")
    path.write_text(content.replace(old, new, 1))
    return True


def ensure_idle():
    for job in JOBS:
        rows = json.loads(command([
            "gcloud", "run", "jobs", "executions", "list", "--job=" + job,
            "--project=" + PROJECT, "--region=" + REGION, "--format=json"]))
        for row in rows:
            status = row.get("status", {})
            completed = next((c.get("status") for c in status.get("conditions", [])
                              if c.get("type") == "Completed"), None)
            if not status.get("completionTime") and completed not in ("True", "False"):
                raise RuntimeError("JOB_STILL_RUNNING:" + row.get("metadata", {}).get("name", job))


def deployment_version(script_id, deployment_id, cwd):
    rows = clasp("list-deployments", script_id, cwd=cwd)
    selected = [x for x in rows if x.get("deploymentId") == deployment_id]
    if len(selected) != 1 or not isinstance(selected[0].get("versionNumber"), int):
        raise RuntimeError("EXISTING_VERSIONED_DEPLOYMENT_NOT_FOUND")
    return selected[0]["versionNumber"]


def clone(script_id, directory, version=None):
    directory.mkdir()
    args = ["clone-script", script_id]
    if version is not None:
        args.append(str(version))  # clasp 3.4.1 takes the version as a positional argument.
    clasp(*args, cwd=directory)


def main():
    os.umask(0o077)
    work = Path(tempfile.mkdtemp(prefix="hunter-monthly-retry-"))
    print("BACKUP=" + str(work), flush=True)
    command(CLASP + ["show-authorized-user", "--json"], work)
    ensure_idle()
    url = secret("APPS_SCRIPT_WEBAPP_URL").strip()
    match = re.fullmatch(r"https://script\.google\.com/macros/s/([A-Za-z0-9_-]+)/exec", url)
    if not match:
        raise RuntimeError("BRIDGE_URL_INVALID")
    deployment_id = match[1]
    scripts = clasp("list-scripts", cwd=work)
    selected = [x for x in scripts if x.get("name") == "HUNTER_GLOBAL_BRIDGE"]
    if len(selected) != 1:
        raise RuntimeError("BRIDGE_PROJECT_NOT_UNIQUE")
    script_id = selected[0]["id"]
    old_version = deployment_version(script_id, deployment_id, work)
    print("READ_EXISTING_DEPLOYMENT version=" + str(old_version), flush=True)
    head, active = work / "head", work / "active"
    clone(script_id, head)
    clone(script_id, active, old_version)
    baseline = source_files(head)
    if baseline != source_files(active):
        raise RuntimeError("UNDEPLOYED_SCRIPT_CHANGES_PRESENT")
    old = monthly_function(command(["git", "show", BASE + ":bridge/Gateway.gs"], ROOT))
    new = monthly_function((ROOT / "bridge/Gateway.gs").read_text())
    staged = work / "staged"
    shutil.copytree(head, staged)
    settings_path = staged / ".clasp.json"
    settings = json.loads(settings_path.read_text())
    settings["rootDir"] = "."
    settings_path.write_text(json.dumps(settings))
    changed = patch_monthly(staged, old, new)
    spec = importlib.util.spec_from_file_location("bridge_post", ROOT / "cloudrun/apps-script-post.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    key = secret("APPS_SCRIPT_SHARED_KEY").strip()

    def verify_bridge():
        doc = bridge.post_json(url, {"op": "month_status", "key": key}, attempts=3)
        if not doc.get("ok") or doc.get("trigger", {}).get("count") != 1:
            raise RuntimeError("MONTHLY_TRIGGER_READBACK_FAILED")
        return doc["trigger"]

    before = verify_bridge()
    if not changed:
        print("MONTHLY_RETRY_ALREADY_DEPLOYED", flush=True)
        return
    # Recheck before push so unrelated edits made during preparation are not overwritten.
    latest = work / "latest"
    clone(script_id, latest)
    if source_files(latest) != baseline or deployment_version(script_id, deployment_id, work) != old_version:
        raise RuntimeError("SCRIPT_CHANGED_DURING_PREPARATION")
    ensure_idle()
    print("DEPLOY_MONTHLY_RETRY_ONLY", flush=True)
    try:
        clasp("push", "--force", cwd=staged)
        deployed = clasp("update-deployment", deployment_id,
                         "--description", "Hunter monthly retry until dual-market commit", cwd=staged)
        new_version = deployed.get("versionNumber")
        if not isinstance(new_version, int) or new_version == old_version:
            raise RuntimeError("NEW_VERSION_NOT_CONFIRMED")
        check = work / "verified"
        clone(script_id, check, new_version)
        if source_files(check) != source_files(staged):
            raise RuntimeError("DEPLOYED_SOURCE_READBACK_MISMATCH")
        if deployment_version(script_id, deployment_id, work) != new_version:
            raise RuntimeError("DEPLOYMENT_VERSION_READBACK_MISMATCH")
        after = verify_bridge()
        if after.get("expectedMonth") != before.get("expectedMonth"):
            raise RuntimeError("MONTHLY_EXPECTED_MONTH_CHANGED")
    except Exception:
        print("ROLLBACK_MONTHLY_DEPLOYMENT", flush=True)
        clasp("update-deployment", deployment_id, "--versionNumber", str(old_version), cwd=head)
        clasp("push", "--force", cwd=head)
        raise
    print("MONTHLY_RETRY_DEPLOYED_AND_VERIFIED version=" + str(new_version), flush=True)
    print("MONTH_TRIGGER=" + json.dumps(after, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc))
