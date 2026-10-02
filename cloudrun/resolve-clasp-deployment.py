#!/usr/bin/env python3
"""Resolve the production Apps Script deployment from clasp list-deployments."""
from __future__ import annotations

import argparse
import re
import sys

LINE = re.compile(r"^\s*-\s+(\S+)\s+@(\S+)\s*$")


def resolve(text: str, preferred: str | None = None) -> str:
    rows: list[tuple[str, int]] = []
    for raw in text.splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        deployment_id, version = m.groups()
        if version.isdigit():
            rows.append((deployment_id, int(version)))
    if not rows:
        raise ValueError("NO_VERSIONED_CLASP_DEPLOYMENT")
    ids = {deployment_id for deployment_id, _ in rows}
    if preferred and preferred in ids:
        return preferred
    rows.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return rows[0][0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preferred")
    args = parser.parse_args()
    try:
        print(resolve(sys.stdin.read(), args.preferred))
    except ValueError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
