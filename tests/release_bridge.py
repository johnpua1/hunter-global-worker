"""Live Bridge release gate using a 2026-09-25 BASE row in _BRIDGE_TEST.

Requires APPS_SCRIPT_WEBAPP_URL and APPS_SCRIPT_SHARED_KEY in the environment.
The script never prints the URL, key, request bodies, or row contents.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import sys
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from runner import Drive, digest, lines_gz, parse_lines_gz


def submit(session: requests.Session, url: str, key: str, op: str,
           path: str, payload: bytes) -> dict:
    response = session.post(url, timeout=120, json={
        "op": op, "path": path, "key": key,
        "data_base64": base64.b64encode(payload).decode("ascii"),
        "sha256": digest(payload), "mime": "application/x-gzip",
    })
    response.raise_for_status()
    return response.json()


def require_error(result: dict, expected: str) -> None:
    if result.get("ok") or result.get("error") != expected:
        raise RuntimeError("GATE_EXPECTED_" + expected)


def sample_2026_09_25(drive: Drive, market: str) -> dict:
    checkpoint = drive.json(f"{market}/CHECKPOINT.json")
    if checkpoint["as_of"] != "2026-09-25" or len(checkpoint["verified_batches"]) != checkpoint["total_batches"]:
        raise RuntimeError("BASE_CHECKPOINT_NOT_VERIFIED:" + market)
    for number in range(1, checkpoint["total_batches"] + 1):
        rows = parse_lines_gz(drive.read(f"{market}/BASE/batch-{number:04d}.ndjson.gz"))
        for row in rows:
            if row["date"] == "2026-09-25":
                return {**row, "trade_date": "2026-09-25"}
    raise RuntimeError("SAMPLE_2026_09_25_ABSENT:" + market)


def main() -> None:
    drive = Drive()
    drive.health()
    session = requests.Session()
    sample = {market: sample_2026_09_25(drive, market) for market in ("US", "HK")}
    base_path = "US/BASE/batch-0001.ndjson.gz"
    base = drive.read(base_path)
    original_sha = hashlib.sha256(base).hexdigest()
    require_error(submit(session, drive.url, drive.key, "put", base_path, base), "BASE_SEALED")
    if hashlib.sha256(drive.read(base_path)).hexdigest() != original_sha:
        raise RuntimeError("BASE_SHA_CHANGED")
    print("BASE_SEAL=PASS")

    run = uuid.uuid4().hex
    for market in ("US", "HK"):
        payload = lines_gz([sample[market]])
        directory = f"_BRIDGE_TEST/DAILY/{market}/2026-09-25/{run}"
        first = f"{directory}/part-0001.ndjson.gz"
        result = submit(session, drive.url, drive.key, "append", first, payload)
        if not result.get("ok") or drive.read(first) != payload:
            raise RuntimeError("DAILY_FIRST_APPEND_FAILED:" + market)
        require_error(submit(session, drive.url, drive.key, "append", first, payload), "APPEND_CONFLICT")
        require_error(submit(session, drive.url, drive.key, "append",
                             f"{directory}/part-0002.ndjson.gz", payload), "DAILY_DUPLICATE_KEY")
        if len([x for x in drive.list(directory) if x["name"].endswith(".ndjson.gz")]) != 1:
            raise RuntimeError("DAILY_REPEAT_ADDED_ROW:" + market)

        race_dir = f"_BRIDGE_TEST/DAILY/{market}/2026-09-25/{uuid.uuid4().hex}"
        paths = [f"{race_dir}/part-{i:04d}.ndjson.gz" for i in (1, 2)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(submit, requests.Session(), drive.url, drive.key,
                                   "append", path, payload) for path in paths]
            answers = [future.result() for future in pending]
        if sum(bool(x.get("ok")) for x in answers) != 1 or not any(
                x.get("error") == "DAILY_DUPLICATE_KEY" for x in answers):
            raise RuntimeError("DAILY_RACE_NOT_SERIALIZED:" + market)
        if len([x for x in drive.list(race_dir) if x["name"].endswith(".ndjson.gz")]) != 1:
            raise RuntimeError("DAILY_RACE_ROW_COUNT:" + market)
        print(f"{market}_DAILY_APPEND_REPEAT_RACE=PASS")

    negative_path = f"US/DAILY/2026-09-25/release-put-denied-{run}.ndjson.gz"
    require_error(submit(session, drive.url, drive.key, "put", negative_path,
                         lines_gz([sample["US"]])), "DAILY_APPEND_ONLY")
    if drive.file(negative_path):
        raise RuntimeError("DAILY_PUT_CREATED_FILE")
    print("DAILY_APPEND_ONLY=PASS")


if __name__ == "__main__":
    main()
