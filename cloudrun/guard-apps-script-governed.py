#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

SCRIPT_EXTS={".js",".gs",".ts"}
GOVERNED=(
    "dailyUS","dailyHK","hunterDailyWatchdog","hunterDailyWatchdogMarket_",
    "installHunterDailyTriggers","runHunterJob_","hunterRecoverStaleExecutions_",
    "hunterJobStaleAfterMs_"
)
FORBIDDEN_LITERALS=("HUNTER_ALERT_LOG_WRITE_FAILED",)


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("script_dir")
    args=parser.parse_args()
    root=Path(args.script_dir)
    if not root.is_dir():
        raise SystemExit(f"SCRIPT_DIR_NOT_FOUND:{root}")

    gateway=root/"Gateway.gs"
    if not gateway.is_file():
        raise SystemExit("GATEWAY_GS_MISSING")
    gateway_text=gateway.read_text(encoding="utf-8")
    if "function hunterDailyWatchdog()" not in gateway_text:
        raise SystemExit("CANONICAL_WATCHDOG_MISSING")

    removed=[]
    for ext in SCRIPT_EXTS:
        p=root/("Watchdog"+ext)
        if p.exists():
            p.unlink()
            removed.append(p.name)
    if removed:
        print("LEGACY_WATCHDOG_REMOVED=" + ",".join(sorted(removed)))
    else:
        print("LEGACY_WATCHDOG_REMOVED=NONE")

    offenders=[]
    function_re=re.compile(r"\bfunction\s+("+"|".join(re.escape(x) for x in GOVERNED)+r")\s*\(")
    for p in sorted(root.iterdir()):
        if p==gateway or p.suffix not in SCRIPT_EXTS or not p.is_file():
            continue
        text=p.read_text(encoding="utf-8")
        names=sorted(set(function_re.findall(text)))
        literals=[x for x in FORBIDDEN_LITERALS if x in text]
        if names or literals:
            offenders.append({
                "file":p.name,
                "functions":names,
                "literals":literals,
            })
    if offenders:
        detail=" | ".join(
            f"{o['file']}:functions={','.join(o['functions']) or '-'}:literals={','.join(o['literals']) or '-'}"
            for o in offenders
        )
        raise SystemExit("GOVERNED_APPS_SCRIPT_COLLISION:" + detail)

    for ext in SCRIPT_EXTS:
        if (root/("Watchdog"+ext)).exists():
            raise SystemExit("LEGACY_WATCHDOG_STILL_PRESENT")
    print("GOVERNED_APPS_SCRIPT_SYMBOLS=PASS")


if __name__=="__main__":
    main()
