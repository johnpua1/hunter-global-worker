#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

# Single-writer recovery release. This script never dispatches a job.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
test "$(git branch --show-current)" = main
git diff --quiet
git diff --cached --quiet
SHA="$(git rev-parse HEAD)"
export GCP_PROJECT_ID=rgs-hunter-global

# Release the same pinned image to the existing US and HK DAILY jobs.
# Each image-only update preserves runtime identity, args, limits and budget;
# HK changes fail closed unless every prior execution is proven terminal.
python cloudrun/deploy-us-phase2-fix-20261015.py "$SHA"
python cloudrun/rebind-hk-existing-image.py "$SHA"

# Update the existing versioned Apps Script bridge in place. Existing primary
# and hourly watchdog trigger counts are read back; monthly deploy is skipped.
bash cloudrun/deploy-daily-control-plane.sh

# Explicitly prove the new evidence-gated watchdog was published. The earlier
# generic bridge-ready / trigger-count probe cannot establish code identity.
BRIDGE_URL="$(gcloud secrets versions access latest \
  --secret=APPS_SCRIPT_WEBAPP_URL --project="$GCP_PROJECT_ID")"
LEGACY_KEY="$(gcloud secrets versions access latest \
  --secret=APPS_SCRIPT_SHARED_KEY --project="$GCP_PROJECT_ID")"
export BRIDGE_URL LEGACY_KEY
ok=0
for attempt in $(seq 1 12); do
  if OUT="$(python cloudrun/apps-script-post.py --op daily_recovery_version --raw 2>/dev/null)"; then
    if printf '%s' "$OUT" | python -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit(1)
raise SystemExit(0 if (
    d.get("ok") is True and
    d.get("version") == "HUNTER_DAILY_CLOSED_LOOP_20261016" and
    d.get("completionGate") == "RANK_PHASE2_AND_NEWER_RECEIPT") else 1)
'; then
      ok=1
      break
    fi
  fi
  sleep 5
done
unset BRIDGE_URL LEGACY_KEY OUT
if [[ "$ok" != 1 ]]; then
  echo "HUNTER_DAILY_WATCHDOG_VERSION_READBACK_FAILED" >&2
  exit 1
fi
echo "HUNTER_AUTOMATION_DEPLOYMENT=VERIFIED"
echo "SOURCE_SHA=$SHA"
echo "NO_EXTRA_SCHEDULER;NO_EXECUTION_DISPATCHED;NO_PRODUCTION_DATA_REWRITTEN"
echo "NEXT_SCHEDULED_EXECUTION_MUST_STILL_PASS_DATA_CHECKPOINT_ACCEPTANCE"
