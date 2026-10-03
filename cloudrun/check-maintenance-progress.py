#!/usr/bin/env python3
"""Read latest Maintenance execution and logs; display timestamps in MYT."""
import datetime as dt
import json
import subprocess
from zoneinfo import ZoneInfo

MYT = ZoneInfo("Asia/Kuala_Lumpur")
PROJECT = "rgs-hunter-global"


def read(args):
    result = subprocess.run(["gcloud"] + args + ["--project=" + PROJECT, "--format=json"],
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("READ_FAILED:" + " ".join(args[:3]))
    return json.loads(result.stdout)


def myt(value):
    if not value:
        return "-"
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(MYT).strftime("%m-%d %H:%M:%S MYT")


def main():
    rows = read(["run", "jobs", "executions", "list", "--job=hunter-maintenance",
                 "--region=us-central1", "--sort-by=~metadata.creationTimestamp", "--limit=1"])
    if not rows:
        raise RuntimeError("NO_MAINTENANCE_EXECUTION")
    row = rows[0]
    name = row.get("metadata", {}).get("name", "")
    if not name.startswith("hunter-maintenance-"):
        raise RuntimeError("EXECUTION_NAME_UNCONFIRMED")
    status = row.get("status", {})
    completed = next((x for x in status.get("conditions", []) if x.get("type") == "Completed"), {})
    state = {"True": "SUCCEEDED", "False": "FAILED"}.get(completed.get("status"), "RUNNING_OR_PENDING")
    print("CHECK_AT=" + dt.datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S MYT"), flush=True)
    print("EXECUTION=" + name + " STATE=" + state, flush=True)
    print("START=" + myt(status.get("startTime")) + " END=" + myt(status.get("completionTime")), flush=True)
    query = ('resource.type="cloud_run_job" AND resource.labels.job_name="hunter-maintenance" AND '
             'labels."run.googleapis.com/execution_name"="' + name + '"')
    logs = read(["logging", "read", query, "--order=desc", "--freshness=24h", "--limit=20"])
    if not logs:
        print("NO_VISIBLE_LOGS_IN_LAST_24H", flush=True)
    for log in reversed(logs):
        message = log.get("textPayload") or json.dumps(log.get("jsonPayload", {}), ensure_ascii=False)
        print(myt(log.get("timestamp")) + " " + message[:1500], flush=True)
    print("EXECUTION_STATUS_IS_NOT_QUEUE_VALIDATION", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc))
