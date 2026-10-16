#!/usr/bin/env bash
set -euo pipefail
set +x

# One release for the existing US Cloud Run image and Apps Script watchdog.
# No Cloud Run execution, BASE rebuild, GitHub daily dispatch or new Scheduler.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
: "${1:?Usage: bash cloudrun/release-hunter-closed-loop-20261016.sh <MERGED_MAIN_SHA>}"
SHA="$1"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "PINNED_SHA_REQUIRED" >&2; exit 1; }
[[ "$(git rev-parse HEAD)" == "$SHA" ]] || { echo "PINNED_CHECKOUT_REQUIRED" >&2; exit 1; }
[[ "$(git branch --show-current)" == "main" ]] || { echo "MAIN_BRANCH_REQUIRED" >&2; exit 1; }
git merge-base --is-ancestor 31fd1bb8cf930c2749207993bc4451f0120dcc39 "$SHA" || {
  echo "CLOSED_LOOP_COMMIT_MISSING" >&2; exit 1;
}
export GCP_PROJECT_ID="${GCP_PROJECT_ID:-rgs-hunter-global}"

# Existing guarded US image-only deploy. Does not execute any jobs.
python cloudrun/deploy-us-phase2-fix-20261015.py "$SHA"

# Existing guarded clasp release, with monthly Cloud Run deployment skipped.
# Preserves the single Apps Script dailyUS/dailyHK/Watchdog topology.
SKIP_MONTHLY_DEPLOY=1 EXPECTED_SHA="$SHA" bash cloudrun/deploy-daily-control-plane.sh

# Require the new actual versioned Apps Script deployment to serve the
# evidence-gated status contract before declaring this release completed.
BRIDGE_URL="$(gcloud secrets versions access latest \
  --secret APPS_SCRIPT_WEBAPP_URL --project "$GCP_PROJECT_ID")"
LEGACY_KEY="$(gcloud secrets versions access latest \
  --secret APPS_SCRIPT_SHARED_KEY --project "$GCP_PROJECT_ID")"
export BRIDGE_URL LEGACY_KEY
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if python cloudrun/apps-script-post.py --op daily_commit_status --raw | \
      python -c '
import json,sys
doc=json.load(sys.stdin)
valid=(doc.get("ok") is True and
       doc.get("release")=="HUNTER_DAILY_CLOSED_LOOP_20261016" and
       all(isinstance(doc.get(m),dict) and
           isinstance(doc[m].get("complete"),bool) for m in ("US","HK")))
raise SystemExit(0 if valid else 1)
' ; then
    ready=1
    break
  fi
  sleep 5
done
unset LEGACY_KEY BRIDGE_URL
[[ "$ready" == "1" ]] || { echo "CLOSED_LOOP_GATEWAY_READBACK_FAILED" >&2; exit 1; }
echo "CLOSED_LOOP_DEPLOY_VERIFIED SHA=$SHA"
echo "NO_JOB_EXECUTIONS_STARTED;NO_EXTRA_SCHEDULERS;NEXT_AUTOMATIC_RUN_PENDING"
