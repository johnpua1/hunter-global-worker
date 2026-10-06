"""Exercise DAILY inputs without permitting Bridge writes."""
import collections
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hunter-global"))
from runner import Drive, load_market, parse_lines_gz
from foundation import current_universe, read_existing
from derived import read_files


class ReadOnlyDrive(Drive):
    deadline = time.monotonic() + 480
    calls = collections.Counter()

    def _call(self, op, **fields):
        if op not in {"file", "list", "read", "read_chunk"}:
            raise RuntimeError("READONLY_WRITE_BLOCKED")
        if time.monotonic() > self.deadline:
            raise RuntimeError("PROBE_DEADLINE")
        start = time.monotonic()
        self.calls[op] += 1
        try:
            result = super()._call(op, **fields)
        except Exception as exc:
            response = getattr(exc, "response", None)
            print(json.dumps({"event": "READ_FAILED", "op": op,
                              "path": fields.get("path"),
                              "offset": fields.get("offset"),
                              "seconds": round(time.monotonic() - start, 2),
                              "type": type(exc).__name__,
                              "http": getattr(response, "status_code", None)}), flush=True)
            raise
        elapsed = time.monotonic() - start
        if elapsed > 10:
            print(json.dumps({"event": "SLOW_READ", "op": op,
                              "path": fields.get("path"),
                              "seconds": round(elapsed, 2)}), flush=True)
        return result


def stage(market, name, fn):
    start = time.monotonic()
    print(json.dumps({"event": "START", "market": market, "stage": name}), flush=True)
    try:
        result = fn()
        print(json.dumps({"event": "PASS", "market": market, "stage": name,
                          "seconds": round(time.monotonic() - start, 2),
                          "result": result}), flush=True)
    except Exception as exc:
        print(json.dumps({"event": "FAIL", "market": market, "stage": name,
                          "seconds": round(time.monotonic() - start, 2),
                          "type": type(exc).__name__}), flush=True)


if __name__ == "__main__":
    os.environ.update(HUNTER_BRIDGE_ATTEMPTS="2",
                      HUNTER_BRIDGE_READ_TIMEOUT_SECONDS="25",
                      HUNTER_BRIDGE_WRITE_TIMEOUT_SECONDS="25",
                      DAILY_READ_WORKERS="2", DERIVED_READ_WORKERS="2")
    drive = ReadOnlyDrive()
    for market in ("US", "HK"):
        def daily_input():
            base = load_market(drive, market)
            securities = current_universe(drive, market)
            last, keys = read_existing(drive, market, securities, base.checkpoint["as_of"])
            return {"securities": len(securities), "stored_keys": len(keys),
                    "last_dates": dict(collections.Counter(last.values()))}
        stage(market, "daily_inputs", daily_input)
    for market in ("HK", "US"):
        stage(market, "derived_base_sample", lambda: {
            "rows": len(parse_lines_gz(drive.read(f"{market}/BASE/batch-0001.ndjson.gz")))})
        def patch_sample():
            paths = read_files(drive, market, "REPAIR_PATCH", ".json")
            sizes = []
            for path in paths[:3] + paths[-3:]:
                raw = drive.read(path)
                json.loads(raw)
                sizes.append(len(raw))
            return {"patch_files": len(paths), "sample_bytes": sizes}
        stage(market, "derived_patch_inventory", patch_sample)
    print(json.dumps({"event": "FINISHED", "calls": dict(drive.calls)}), flush=True)
