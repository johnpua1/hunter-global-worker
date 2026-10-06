"""Restore the existing US common-share filter without removing any identities."""
import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hunter-global"))
from runner import Drive, compact, digest, now_myt
from universe import us_exclusion


def repair(drive, apply=False):
    path = "US/CURRENT_UNIVERSE.json"
    original = drive.read(path)
    doc = json.loads(original)
    if doc.get("market") != "US":
        raise RuntimeError("UNIVERSE_MARKET_MISMATCH")
    changes = []
    for sec in doc["securities"]:
        if sec.get("listing_status") != "ACTIVE":
            continue
        status = us_exclusion(sec.get("name", ""))
        if not status:
            continue
        fields = ("listing_status", "review_status", "exclusion_reason")
        changes.append({"security_id": sec["security_id"],
                        "before": {key: sec[key] for key in fields if key in sec},
                        "absent_before": [key for key in fields if key not in sec],
                        "after_status": status})
        sec.update(listing_status=status, review_status="EXCLUDED",
                   exclusion_reason="OFFICIAL_LISTING_SECURITY_TYPE_EXCLUDED")
    changed_ids = {c["security_id"] for c in changes}
    summary = {"market": "US", "changes": len(changes), "apply": apply,
               "active_after": sum(x.get("listing_status") == "ACTIVE" for x in doc["securities"]),
               "new_listing_excluded": sum(
                   x.get("security_id_origin") == "NEW_LISTING" and
                   x["security_id"] in changed_ids
                   for x in doc["securities"]),
               "statuses": dict(collections.Counter(c["after_status"] for c in changes))}
    if not changes or not apply:
        return summary
    # Keep the prior official-list refresh time. This is a correction, not a
    # new source observation. The receipt contains reversible field changes.
    repaired = compact(doc)
    receipt = {"kind": "SYNC_ELIGIBILITY_REPAIR", "updated_at_myt": now_myt(),
               "before_sha256": digest(original), "after_sha256": digest(repaired),
               "changes": changes}
    receipt_path = "US/CONTROL/SYNC_ELIGIBILITY_REPAIR_" + digest(original)[:16] + ".json"
    if not drive.file(receipt_path):
        drive.put(receipt_path, compact(receipt), immutable=True)
    drive.put(path, repaired, expected_sha=digest(original))
    summary["readback_sha256"] = digest(drive.read(path))
    if summary["readback_sha256"] != digest(repaired):
        raise RuntimeError("UNIVERSE_REPAIR_READBACK_FAILED")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair(Drive(), args.apply), sort_keys=True), flush=True)
