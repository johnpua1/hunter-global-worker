#!/usr/bin/env python3
"""Normalize clasp script-source extensions so each basename is unique."""
from __future__ import annotations

import argparse
from pathlib import Path

SCRIPT_EXTS = (".gs", ".js", ".ts")
PREFERENCE = {".gs": 0, ".js": 1, ".ts": 2}


def _groups(root: Path):
    groups = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SCRIPT_EXTS:
            continue
        key = str(path.relative_to(root).with_suffix(""))
        groups.setdefault(key, []).append(path)
    return groups


def normalize(root: Path) -> list[tuple[str, str]]:
    removed: list[tuple[str, str]] = []
    for key, paths in sorted(_groups(root).items()):
        if len(paths) < 2:
            continue
        keep = min(paths, key=lambda p: (PREFERENCE[p.suffix.lower()], p.name))
        for path in sorted(paths):
            if path == keep:
                continue
            path.unlink()
            removed.append((str(path.relative_to(root)), str(keep.relative_to(root))))
    return removed


def assert_unique(root: Path) -> None:
    conflicts = []
    for key, paths in sorted(_groups(root).items()):
        if len(paths) > 1:
            conflicts.append(f"{key}: " + ", ".join(str(p.relative_to(root)) for p in sorted(paths)))
    if conflicts:
        raise SystemExit("CONFLICTING_CLASP_BASENAMES: " + " | ".join(conflicts))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--normalize", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"CLASP_DIR_NOT_FOUND:{root}")
    if args.normalize:
        for removed, kept in normalize(root):
            print(f"CLASP_DEDUP removed={removed} kept={kept}")
    assert_unique(root)
    print("CLASP_BASENAME_UNIQUE=PASS")


if __name__ == "__main__":
    main()
