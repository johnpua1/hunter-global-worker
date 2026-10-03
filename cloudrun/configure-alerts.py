#!/usr/bin/env python3
"""Idempotent Hunter error alerts. No job, trigger, IAM or data changes."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

PROJECT = "rgs-hunter-global"
REGION = "us-central1"
JOBS = ("hunter-us-daily", "hunter-hk-daily", "hunter-maintenance", "hunter-monthly-v2")
MANAGED = {"managed_by": "hunter-alerts-v1"}
API = "https://monitoring.googleapis.com/v3/"


def command(args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("COMMAND_FAILED:" + " ".join(args[:3]))
    return result.stdout.strip()


class Monitoring:
    def __init__(self):
        self.token = command(["gcloud", "auth", "print-access-token"])

    def request(self, method, path, body=None):
        req = urllib.request.Request(API + path,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # No token or server response dump in the terminal.
            raise RuntimeError("MONITORING_HTTP_" + str(exc.code) + ":" + method + ":" + path.split("?")[0]) from None

    def list(self, kind):
        rows, token = [], ""
        while True:
            path = "projects/" + PROJECT + "/" + kind + "?pageSize=100"
            if token:
                path += "&pageToken=" + urllib.parse.quote(token, safe="")
            response = self.request("GET", path)
            rows.extend(response.get(kind, []))
            token = response.get("nextPageToken")
            if not token:
                return rows


def email_address(value):
    value = value.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) or value.endswith(".gserviceaccount.com"):
        raise RuntimeError("HUMAN_EMAIL_REQUIRED: use --email your-address")
    return value


def definitions(channel, scheduler_names):
    rows = []
    for job in JOBS:
        query = ('resource.type="cloud_run_job" AND resource.labels.job_name="' + job + '" AND '
                 '(severity>=ERROR OR textPayload=~"HUNTER_CONFIG_BLOCKED|MAINTENANCE_REPAIR_INCOMPLETE|DAILY_INCOMPLETE|Traceback ")')
        rows.append((job, query))
    control = ('log_id("hunter-control") AND severity>=ERROR')
    if scheduler_names:
        names = " OR ".join('resource.labels.job_id=' + json.dumps(name) for name in scheduler_names)
        control = "(" + control + ") OR (resource.type=\"cloud_scheduler_job\" AND (" + names + ") AND severity>=ERROR)"
    rows.append(("control", control))
    return [{
        "displayName": "Hunter errors: " + name,
        "userLabels": MANAGED,
        "enabled": True,
        "combiner": "OR",
        "conditions": [{"displayName": "Hunter error log", "conditionMatchedLog": {"filter": query}}],
        "alertStrategy": {"notificationRateLimit": {"period": "300s"}, "autoClose": "3600s"},
        "notificationChannels": [channel],
        "documentation": {"mimeType": "text/markdown", "content":
            "Hunter 检测到错误：" + name + "。请查看日志及 execution 最终状态；重试成功前不能视为已修复。\n"
            "业务时间基准：Asia/Kuala_Lumpur（MYT，UTC+8）；Google 通知可能使用账户时区。\n"
            "此规则监测错误日志，不证明数据已更新，也不覆盖完全没有触发的漏跑。"}
    } for name, query in rows]


def comparable(policy):
    fields = ("displayName", "userLabels", "enabled", "combiner", "conditions", "alertStrategy",
              "notificationChannels", "documentation")
    result = {key: policy.get(key) for key in fields}
    result["conditions"] = [{key: value for key, value in condition.items() if key != "name"}
                            for condition in result["conditions"] or []]
    for condition in result["conditions"]:
        matched = dict(condition.get("conditionMatchedLog", {}))
        if not matched.get("labelExtractors"):
            matched.pop("labelExtractors", None)
        condition["conditionMatchedLog"] = matched
    strategy = policy.get("alertStrategy", {})
    result["alertStrategy"] = {key: strategy.get(key) for key in ("notificationRateLimit", "autoClose")}
    documentation = policy.get("documentation", {})
    result["documentation"] = {key: documentation.get(key) for key in ("content", "mimeType")}
    return result


def select_managed(rows, display_name):
    selected = [row for row in rows if row.get("displayName") == display_name]
    if len(selected) > 1 or any(row.get("userLabels") != MANAGED for row in selected):
        raise RuntimeError("UNMANAGED_OR_DUPLICATE_ALERT:" + display_name)
    return selected[0] if selected else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="Defaults to the active human gcloud account")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    email = email_address(args.email or command(["gcloud", "config", "get-value", "account"]))
    schedules = json.loads(command(["gcloud", "scheduler", "jobs", "list", "--project=" + PROJECT,
                                   "--location=" + REGION, "--format=json"]))
    names = [row["name"].rsplit("/", 1)[-1] for row in schedules
             if row.get("httpTarget", {}).get("uri", "").endswith("/hunter-maintenance:run")]
    if len(names) != 1:
        raise RuntimeError("MAINTENANCE_SCHEDULER_NOT_UNIQUE")
    client = Monitoring()
    channels, policies = client.list("notificationChannels"), client.list("alertPolicies")
    channel_title = "Hunter owner email"
    existing_channel = select_managed(channels, channel_title)
    if existing_channel and existing_channel.get("labels", {}).get("email_address") != email:
        raise RuntimeError("EXISTING_ALERT_RECIPIENT_DIFFERS")
    desired_channel = {"displayName": channel_title, "type": "email", "labels": {"email_address": email},
                       "userLabels": MANAGED, "enabled": True}
    planned = definitions(existing_channel["name"] if existing_channel else "PENDING_CHANNEL", names)
    for policy in planned:
        select_managed(policies, policy["displayName"])
    print("ALERT_RECIPIENT=" + email, flush=True)
    print("ALERT_PLAN policies=5 jobs=4 control=1", flush=True)
    if not args.apply:
        print("PLAN_ONLY: rerun with --apply", flush=True)
        return
    os.umask(0o077)
    backup = Path(tempfile.mkdtemp(prefix="hunter-alert-backup-")) / "before.json"
    backup.write_text(json.dumps({"channels": channels, "policies": policies}, ensure_ascii=False, indent=2))
    print("BACKUP=" + str(backup), flush=True)
    if existing_channel:
        if existing_channel.get("type") != "email" or not existing_channel.get("enabled", True):
            raise RuntimeError("EXISTING_CHANNEL_DISABLED_OR_WRONG_TYPE")
        channel = existing_channel
    else:
        channel = client.request("POST", "projects/" + PROJECT + "/notificationChannels", desired_channel)
    # Read the destination back before connecting any policy to it.
    checked = client.request("GET", channel["name"])
    if (checked.get("labels", {}).get("email_address") != email or checked.get("type") != "email"
            or not checked.get("enabled", True)):
        raise RuntimeError("CHANNEL_READBACK_FAILED")
    if checked.get("verificationStatus") == "UNVERIFIED":
        raise RuntimeError("EMAIL_CHANNEL_REQUIRES_VERIFICATION")
    fields = "displayName,userLabels,enabled,combiner,conditions,alertStrategy,notificationChannels,documentation"
    for desired in definitions(channel["name"], names):
        existing = select_managed(policies, desired["displayName"])
        if existing and comparable(existing) == comparable(desired):
            result = existing
        elif existing:
            update = json.loads(json.dumps(dict(desired, name=existing["name"])))
            old_conditions = existing.get("conditions", [])
            if len(old_conditions) == 1 and old_conditions[0].get("name"):
                update["conditions"][0]["name"] = old_conditions[0]["name"]
            result = client.request("PATCH", existing["name"] + "?updateMask=" + fields, update)
        else:
            result = client.request("POST", "projects/" + PROJECT + "/alertPolicies", desired)
        actual = client.request("GET", result["name"])
        if comparable(actual) != comparable(desired):
            raise RuntimeError("ALERT_POLICY_READBACK_FAILED:" + desired["displayName"])
        print("VERIFIED " + desired["displayName"], flush=True)
    print("ERROR_ALERTS_CONFIGURED_AND_READ_BACK", flush=True)
    print("EMAIL_DELIVERY_NOT_TESTED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc))
