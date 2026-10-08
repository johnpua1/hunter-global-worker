#!/usr/bin/env bash
(
set -euo pipefail
umask 077
daily_sha="${1:-}"
[[ "$daily_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'PINNED_SHA_REQUIRED'; exit 1; }
daily_work="$(mktemp -d "$HOME/hunter-daily-data.XXXXXX")"
trap 'rm -rf -- "$daily_work"' EXIT
git -C "$daily_work" init -q
GIT_TERMINAL_PROMPT=0 git -C "$daily_work" fetch -q --depth=1 \
  https://github.com/johnpua1/hunter-global-worker.git "$daily_sha"
[[ "$(git -C "$daily_work" rev-parse FETCH_HEAD)" == "$daily_sha" ]] || exit 1
git -C "$daily_work" checkout -q --detach FETCH_HEAD
python3 -u "$daily_work/cloudrun/deploy-daily-data-only.py" "$daily_sha"
)
