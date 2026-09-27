"""Drain the existing Repair queue with the deployed Cloud Run market Jobs.

Run in an authenticated Cloud Shell. Credentials are read from Secret Manager
in memory and are never printed. No BASE or VERIFIED job is invoked.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.request


PROJECT = "rgs-hunter-global"
REGION = "us-central1"


def secret(name: str) -> str:
    proc = subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest",
         f"--secret={name}", f"--project={PROJECT}"],
        capture_output=True, check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"Secret Manager access failed: {name}")
    return proc.stdout.decode("utf-8").strip()


def open_counts(url: str, key: str) -> dict[str, int]:
    request = urllib.request.Request(
        url,
        data=json.dumps({"op": "read", "path": "REPAIR_QUEUE.json", "key": key}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except Exception as exc:
        raise RuntimeError("Bridge queue read failed") from None
    if not result.get("ok"):
        raise RuntimeError("Bridge queue read rejected")
    queue = json.loads(base64.b64decode(result["data_base64"]))
    counts = {"US": 0, "HK": 0}
    for item in queue["items"]:
        if item.get("status", "OPEN") == "OPEN" and item.get("market") in counts:
            counts[item["market"]] += 1
    return counts


def execute(market: str) -> None:
    job = f"hunter-{market.lower()}-daily"
    result = subprocess.run([
        "gcloud", "run", "jobs", "execute", job,
        f"--project={PROJECT}", f"--region={REGION}",
        f"--args=--mode,repair,--market,{market}", "--wait",
    ], check=False)
    if result.returncode:
        raise RuntimeError(f"{market} repair execution failed; queue remains resumable")


def execute_maintenance() -> None:
    result = subprocess.run([
        "gcloud", "run", "jobs", "execute", "hunter-maintenance",
        f"--project={PROJECT}", f"--region={REGION}", "--wait",
    ], check=False)
    if result.returncode:
        raise RuntimeError("Maintenance execution failed; completed repairs remain saved")


def main() -> None:
    url = secret("APPS_SCRIPT_WEBAPP_URL")
    key = secret("APPS_SCRIPT_SHARED_KEY")
    counts = open_counts(url, key)
    print(f"OPEN US={counts['US']} HK={counts['HK']}", flush=True)
    for market in ("US", "HK"):
        while counts[market]:
            previous = counts[market]
            execute(market)
            counts = open_counts(url, key)
            print(f"OPEN US={counts['US']} HK={counts['HK']}", flush=True)
            if counts[market] >= previous:
                raise RuntimeError(f"{market} queue made no progress; inspect source blocker")
    print("REPAIR_QUEUE_OPEN=0", flush=True)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
