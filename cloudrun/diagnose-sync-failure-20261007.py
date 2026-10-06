"""Read existing DAILY execution logs in authenticated Cloud Shell; no mutations."""
import json
import re
import subprocess


def read(*args):
    command = ["gcloud", *args, "--project=rgs-hunter-global", "--format=json", "--quiet"]
    if args[0] == "run":
        command.append("--region=us-central1")
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("READ_FAILED:" + " ".join(args[:3]))
    return json.loads(result.stdout or "[]")


def main():
    for job in ("hunter-us-daily", "hunter-hk-daily"):
        print("JOB=" + job, flush=True)
        executions = read("run", "jobs", "executions", "list", "--job=" + job,
                          "--sort-by=~metadata.creationTimestamp", "--limit=3")
        for execution in executions:
            metadata = execution.get("metadata", {})
            status = execution.get("status", {})
            print(json.dumps({"execution": metadata.get("name"),
                              "created": metadata.get("creationTimestamp"),
                              "finished": status.get("completionTime"),
                              "conditions": [{k: c.get(k) for k in ("type", "status", "reason")}
                                             for c in status.get("conditions", [])]}), flush=True)
        query = ('resource.type="cloud_run_job" AND resource.labels.job_name="' + job +
                 '" AND timestamp>="2026-10-06T22:07:00Z"')
        logs = read("logging", "read", query, "--limit=60", "--order=desc")
        for entry in reversed(logs):
            message = entry.get("textPayload") or entry.get("jsonPayload", {}).get("message", "")
            message = re.sub(r"https?://\S+", "[URL]", str(message))
            message = re.sub(r"(?i)(bearer\s+|(?:key|token|password)[\"']?\s*[:=]\s*)[^\s,}]+",
                             r"\1[REDACTED]", message)
            if message.strip():
                print(entry.get("timestamp", "") + " " + message[:4000], flush=True)


if __name__ == "__main__":
    main()
