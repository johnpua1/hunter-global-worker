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
import sys
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


LAUNCH_BASE = "ff37f68afb1203a9fd9bcd7d80eec2e1fde5532a"
LAUNCH_FUNCTIONS = ("hunterCloudRequest_", "status", "runHunterJob_")


def launch_functions(source):
    result = {}
    for name in LAUNCH_FUNCTIONS:
        matches = re.findall(r"^function " + re.escape(name) + r"\([^\n]*\) \{.*?^\}\n",
                             source, re.M | re.S)
        if len(matches) != 1:
            raise RuntimeError("LAUNCH_FUNCTION_NOT_UNIQUE:" + name)
        result[name] = matches[0]
    return result


def patch_launch(directory, old, new):
    candidates = []
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix in (".js", ".gs"):
            content = path.read_text()
            if re.search(r"function\s+runHunterJob_\s*\(", content):
                candidates.append((path, content))
    if len(candidates) != 1:
        raise RuntimeError("LAUNCH_HANDLER_NOT_UNIQUE")
    path, content = candidates[0]
    actual = launch_functions(content)
    if actual == new:
        return False
    if actual != old:
        raise RuntimeError("LIVE_LAUNCH_DIFFERS_FROM_REVIEWED_BASE")
    for name in LAUNCH_FUNCTIONS:
        content = content.replace(old[name], new[name], 1)
    path.write_text(content)
    return True


def patch_myt_manifest(directory):
    path = directory / "appsscript.json"
    manifest = json.loads(path.read_text())
    previous = manifest.get("timeZone")
    if previous not in ("Asia/Singapore", "Asia/Kuala_Lumpur"):
        raise RuntimeError("UNREVIEWED_SCRIPT_TIMEZONE:" + str(previous))
    if previous != "Asia/Kuala_Lumpur":
        manifest["timeZone"] = "Asia/Kuala_Lumpur"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return previous


def verify_launch_triggers(doc, expected_timezone):
    monthly = doc.get("monthly", {})
    if not doc.get("ok") or monthly.get("count") != 1:
        raise RuntimeError("MONTHLY_TRIGGER_READBACK_FAILED")
    daily = doc.get("daily", {})
    if daily.get("timeZone") != expected_timezone:
        raise RuntimeError("DAILY_TIMEZONE_READBACK_FAILED:" + str(daily.get("timeZone")))
    if daily.get("counts") != {"dailyUS": 1, "dailyHK": 1}:
        raise RuntimeError("DAILY_TRIGGER_COUNT_READBACK_FAILED:" + json.dumps(daily.get("counts")))
    return {"monthly": monthly, "daily": daily}


def expected_myt_triggers(before):
    expected = json.loads(json.dumps(before))
    expected["daily"]["timeZone"] = "Asia/Kuala_Lumpur"
    return expected


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


def main(launch_guard=False):
    os.umask(0o077)
    label = "LAUNCH_GUARD" if launch_guard else "MONTHLY_RETRY"
    work = Path(tempfile.mkdtemp(prefix="hunter-" + label.lower() + "-"))
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
    extractor = launch_functions if launch_guard else monthly_function
    base = LAUNCH_BASE if launch_guard else BASE
    old = extractor(command(["git", "show", base + ":bridge/Gateway.gs"], ROOT))
    new = extractor((ROOT / "bridge/Gateway.gs").read_text())
    staged = work / "staged"
    shutil.copytree(head, staged)
    settings_path = staged / ".clasp.json"
    settings = json.loads(settings_path.read_text())
    settings["rootDir"] = "."
    settings_path.write_text(json.dumps(settings))
    changed = (patch_launch if launch_guard else patch_monthly)(staged, old, new)
    previous_timezone = None
    if launch_guard:
        previous_timezone = patch_myt_manifest(staged)
        changed = changed or previous_timezone != "Asia/Kuala_Lumpur"
    spec = importlib.util.spec_from_file_location("bridge_post", ROOT / "cloudrun/apps-script-post.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    key = secret("APPS_SCRIPT_SHARED_KEY").strip()

    def verify_bridge(expected_timezone="Asia/Kuala_Lumpur"):
        op = "topology_status" if launch_guard else "month_status"
        doc = bridge.post_json(url, {"op": op, "key": key}, attempts=3)
        if launch_guard:
            return verify_launch_triggers(doc, expected_timezone)
        trigger = doc.get("monthly" if launch_guard else "trigger", {})
        if not doc.get("ok") or trigger.get("count") != 1:
            raise RuntimeError("MONTHLY_TRIGGER_READBACK_FAILED")
        return trigger

    before = verify_bridge(previous_timezone) if launch_guard else verify_bridge()
    if not changed:
        print(label + "_ALREADY_DEPLOYED", flush=True)
        return
    # Recheck before push so unrelated edits made during preparation are not overwritten.
    latest = work / "latest"
    clone(script_id, latest)
    if source_files(latest) != baseline or deployment_version(script_id, deployment_id, work) != old_version:
        raise RuntimeError("SCRIPT_CHANGED_DURING_PREPARATION")
    ensure_idle()
    print("DEPLOY_" + label + "_ONLY", flush=True)
    try:
        clasp("push", "--force", cwd=staged)
        deployed = clasp("update-deployment", deployment_id,
                         "--description", "Hunter " + label.lower() + " scoped patch", cwd=staged)
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
        if launch_guard and after != expected_myt_triggers(before):
            raise RuntimeError("TRIGGERS_CHANGED_DURING_DEPLOY")
        if not launch_guard and after.get("expectedMonth") != before.get("expectedMonth"):
            raise RuntimeError("MONTHLY_EXPECTED_MONTH_CHANGED")
    except Exception:
        print("ROLLBACK_" + label + "_DEPLOYMENT", flush=True)
        clasp("update-deployment", deployment_id, "--versionNumber", str(old_version), cwd=head)
        clasp("push", "--force", cwd=head)
        raise
    print(label + "_DEPLOYED_AND_VERIFIED version=" + str(new_version), flush=True)
    print("MONTH_TRIGGER=" + json.dumps(after, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        if sys.argv[1:] not in ([], ["--launch-guard"]):
            raise RuntimeError("USAGE: deploy-monthly-retry.py [--launch-guard]")
        main(launch_guard=sys.argv[1:] == ["--launch-guard"])
    except Exception as exc:
        raise SystemExit(str(exc))
