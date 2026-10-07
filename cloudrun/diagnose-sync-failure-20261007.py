"""Read existing Hunter execution logs in Cloud Shell; no mutations or launches."""
import datetime as dt
import json
import re
import subprocess
import sys

MYT = dt.timezone(dt.timedelta(hours=8))


def myt(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(MYT).isoformat() if value else None


def read(*args):
    command = ["gcloud", *args, "--project=rgs-hunter-global", "--format=json", "--quiet"]
    if args[0] == "run":
        command.append("--region=us-central1")
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("READ_FAILED:" + " ".join(args[:3]))
    return json.loads(result.stdout or "[]")


def main():
    support = '--support-only' in sys.argv
    jobs = ("hunter-maintenance", "hunter-monthly-v2") if support else ("hunter-us-daily", "hunter-hk-daily")
    print('OBSERVED_AT_MYT=' + dt.datetime.now(MYT).isoformat(timespec='seconds'), flush=True)
    for job in jobs:
        print("JOB=" + job, flush=True)
        if support:
            doc = read("run", "jobs", "describe", job)
            task = doc.get('spec', {}).get('template', {}).get('spec', {}).get('template', {}).get('spec', {})
            if not task:
                task = doc.get('template', {}).get('template', {})
            print(json.dumps({'image': [c.get('image') for c in task.get('containers', [])],
                              'timeout': task.get('timeoutSeconds', task.get('timeout')),
                              'retries': task.get('maxRetries')}), flush=True)
        executions = read("run", "jobs", "executions", "list", "--job=" + job,
                          "--sort-by=~metadata.creationTimestamp", "--limit=3")
        for execution in executions:
            metadata = execution.get("metadata", {})
            status = execution.get("status", {})
            print(json.dumps({"execution": metadata.get("name"),
                              "created_myt": myt(metadata.get("creationTimestamp")),
                              "finished_myt": myt(status.get("completionTime")),
                              "conditions": [{k: c.get(k) for k in ("type", "status", "reason")}
                                             for c in status.get("conditions", [])]}), flush=True)
        if not executions:
            print('NO_EXECUTIONS', flush=True)
            continue
        execution_name = executions[0].get('metadata', {}).get('name', '').rsplit('/', 1)[-1]
        if not re.fullmatch(re.escape(job) + r'-[a-z0-9]+', execution_name):
            raise RuntimeError('EXECUTION_NAME_INVALID')
        query = ('resource.type="cloud_run_job" AND resource.labels.job_name="' + job +
                 '" AND labels."run.googleapis.com/execution_name"="' + execution_name + '"')
        logs = read("logging", "read", query, "--limit=60", "--order=desc")
        for entry in reversed(logs):
            message = entry.get("textPayload") or entry.get("jsonPayload", {}).get("message", "")
            message = re.sub(r"https?://\S+", "[URL]", str(message))
            message = re.sub(r"(?i)(bearer\s+|(?:key|token|password)[\"']?\s*[:=]\s*)[^\s,}]+",
                             r"\1[REDACTED]", message)
            if message.strip():
                # Strip the application's duplicate, unzoned UTC prefix.
                message = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ ', '', message)
                print(str(myt(entry.get("timestamp"))) + " " + message[:4000], flush=True)
    if support:
        scheduler = read('scheduler', 'jobs', 'describe', 'hunter-maintenance', '--location=us-central1')
        print('MAINTENANCE_SCHEDULER=' + json.dumps({
            'state': scheduler.get('state'), 'schedule': scheduler.get('schedule'),
            'timeZone': scheduler.get('timeZone'),
            'target': scheduler.get('httpTarget', {}).get('uri'),
            'lastAttemptTime_myt': myt(scheduler.get('lastAttemptTime')),
            'status': scheduler.get('status')}), flush=True)


if __name__ == "__main__":
    main()
