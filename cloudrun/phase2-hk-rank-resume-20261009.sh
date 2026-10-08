#!/usr/bin/env bash
(
set -euo pipefail
umask 077
phase2_sha="${1:-}"
if [[ ! "$phase2_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'STOPPED=请使用包含固定版本号的完整下载执行命令'; exit 1
fi
phase2_work="$(mktemp -d "$HOME/hunter-phase2-closeout.XXXXXX")"
trap 'rm -rf -- "$phase2_work"' EXIT
printf '%s\n' 'PREPARING_PINNED_SOURCE=正在自动准备固定版本收口代码'
mkdir -p "$phase2_work/source"
git -C "$phase2_work/source" init -q
GIT_TERMINAL_PROMPT=0 git -C "$phase2_work/source" fetch -q --depth=1 \
  https://github.com/johnpua1/hunter-global-worker.git "$phase2_sha"
if [ "$(git -C "$phase2_work/source" rev-parse FETCH_HEAD)" != "$phase2_sha" ]; then
  echo 'STOPPED=SOURCE_SHA_MISMATCH'; exit 1
fi
git -C "$phase2_work/source" checkout -q --detach FETCH_HEAD
if ! python3 -c 'import requests' >/dev/null 2>&1; then
  python3 -m pip install --quiet --target "$phase2_work/deps" requests
fi
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$phase2_work/deps${PYTHONPATH:+:$PYTHONPATH}" python3 -u - "$phase2_work/source" "$phase2_sha" <<'PHASE2_PY' 2>&1 | tee -a "$HOME/hunter-phase2-closeout.log"
import pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / 'cloudrun'))
from hk_rank_closeout import main
main(root)
PHASE2_PY
)
