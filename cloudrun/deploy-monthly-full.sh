#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GCP_PROJECT_ID:=rgs-hunter-global}"
REGION="${GCP_REGION:-us-central1}"
CLASP="@google/clasp@3.4.1"
WORK_ROOT="${HOME}/hunter-monthly-release"
SCRIPT_DIR="${WORK_ROOT}/apps-script"
URL_SECRET="APPS_SCRIPT_WEBAPP_URL"
LEGACY_KEY_SECRET="APPS_SCRIPT_SHARED_KEY"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MERGED_MAIN_SHA="$(git rev-parse HEAD)"
if [[ -n "${EXPECTED_SHA:-}" && "$MERGED_MAIN_SHA" != "$EXPECTED_SHA" ]]; then
  echo "HEAD mismatch: expected $EXPECTED_SHA, got $MERGED_MAIN_SHA" >&2
  exit 1
fi
export MERGED_MAIN_SHA GCP_PROJECT_ID GCP_REGION="$REGION"

gcloud config set project "$GCP_PROJECT_ID" >/dev/null
gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . || gcloud auth login --no-launch-browser

rm -rf "$WORK_ROOT"
mkdir -p "$SCRIPT_DIR"

# clasp OAuth is user-scoped and is the only interactive step. Reuse an
# existing login when available; otherwise print the Google authorization URL
# and accept the returned code in Cloud Shell.
if ! npx -y "$CLASP" show-authorized-user --json >/dev/null 2>&1; then
  npx -y "$CLASP" login --no-localhost
fi

SECRET_BRIDGE_URL="$(gcloud secrets versions access latest --secret "$URL_SECRET" --project "$GCP_PROJECT_ID")"
BRIDGE_URL="$SECRET_BRIDGE_URL"
export BRIDGE_URL
SECRET_DEPLOYMENT_ID="$(printf '%s' "$SECRET_BRIDGE_URL" | sed -n 's#^https://script.google.com/macros/s/\([^/]*\)/exec$#\1#p')"
test -n "$SECRET_DEPLOYMENT_ID"

SCRIPTS="$(npx -y "$CLASP" list-scripts)"
SCRIPT_ID="$(printf '%s\n' "$SCRIPTS" | python -c 'import re,sys
rows=[x.strip() for x in sys.stdin if x.strip()]
hits=[]
for x in rows:
    if x.startswith("HUNTER_GLOBAL_BRIDGE"):
        m=re.search(r"[–-]\s*([^\s]+)\s*$",x)
        if m: hits.append(m.group(1))
if len(hits)!=1:
    raise SystemExit("Expected exactly one HUNTER_GLOBAL_BRIDGE in clasp list-scripts; got %d" % len(hits))
print(hits[0])')"

mkdir -p "$SCRIPT_DIR"
cd "$SCRIPT_DIR"
npx -y "$CLASP" clone-script "$SCRIPT_ID"
test -f .clasp.json

# clasp can clone Apps Script sources as .js while this repository owns the
# replacement as .gs. A directory containing both Gateway.js and Gateway.gs is
# rejected as "Conflicting files found". Normalize any duplicate script
# basenames first, then replace the managed Gateway source with exactly one
# extension.
python "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR" --normalize
rm -f "$SCRIPT_DIR/Gateway.js" "$SCRIPT_DIR/Gateway.ts" "$SCRIPT_DIR/Gateway.gs"
cp "$ROOT/bridge/Gateway.gs" "$SCRIPT_DIR/Gateway.gs"
cp "$ROOT/bridge/appsscript.json" "$SCRIPT_DIR/appsscript.json"

# Preserve unrelated remote files (for example Code.js and KeylessBuild.js),
# but fail closed if any basename still exists with more than one script
# extension before push.
python "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR"
python "$ROOT/cloudrun/guard-apps-script-governed.py" "$SCRIPT_DIR"
python - "$ROOT/bridge/Gateway.gs" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
required = (
    "function hunterDailyWatchdog()",
    "HUNTER_DAILY_STATE_V1_",
    "HUNTER_DAILY_WATCHDOG_MAX_ATTEMPTS = 3",
    "newTrigger('hunterDailyWatchdog').timeBased().everyHours(1)",
    "function dailyUS()",
    "function dailyHK()",
    "function runHunterJob_(",
    "function hunterRecoverStaleExecutions_(",
    "function hunterJobStaleAfterMs_(",
    "execution.name + ':cancel'",
    "op === 'daily_watchdog_once'",
    "STALE_CANCEL_ALREADY_TERMINAL",
    "completed && completed.state === 'CONDITION_SUCCEEDED'",
)
missing = [item for item in required if item not in source]
if missing:
    raise SystemExit("BRIDGE_SOURCE_GUARD_FAILED:" + ",".join(missing))
print("BRIDGE_SOURCE_GUARD=PASS")
PY

npx -y "$CLASP" push --force

# Resolve the actual versioned deployment from clasp itself. Ignore @HEAD
# (development deployment). Prefer the ID currently stored in Secret Manager
# when it is still present; otherwise take the highest versioned deployment.
DEPLOYMENTS="$(npx -y "$CLASP" list-deployments)"
printf '%s\n' "$DEPLOYMENTS"
DEPLOYMENT_ID="$(printf '%s\n' "$DEPLOYMENTS" | \
  python "$ROOT/cloudrun/resolve-clasp-deployment.py" --preferred "$SECRET_DEPLOYMENT_ID")"
test -n "$DEPLOYMENT_ID"

# clasp 3.x exposes a dedicated update-deployment command. Use the resolved
# versioned deployment directly instead of create-deployment --deploymentId.
npx -y "$CLASP" update-deployment "$DEPLOYMENT_ID" \
  --description "Hunter V2 monthly bridge ${MERGED_MAIN_SHA}"

BRIDGE_URL="https://script.google.com/macros/s/${DEPLOYMENT_ID}/exec"
export BRIDGE_URL
LEGACY_KEY="$(gcloud secrets versions access latest --secret "$LEGACY_KEY_SECRET" --project "$GCP_PROJECT_ID")"

bridge_get_ok() {
  BRIDGE_URL="$BRIDGE_URL" python - <<'PY'
import json, os, urllib.request
with urllib.request.urlopen(os.environ["BRIDGE_URL"], timeout=30) as r:
    body=json.load(r)
raise SystemExit(0 if body=={"ok":True,"service":"HUNTER_GLOBAL_BRIDGE"} else 1)
PY
}

bridge_post_ok() {
  BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" \
    python "$ROOT/cloudrun/apps-script-post.py" --op file \
      --path "US/CONTROL/DAILY_CHECKPOINT.json" --raw | \
    python -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") else 1)'
}

bridge_ready() {
  for attempt in $(seq 1 12); do
    if bridge_get_ok && bridge_post_ok; then
      return 0
    fi
    sleep 5
  done
  return 1
}

if ! bridge_ready; then
  echo "Existing Apps Script deployment failed GET/POST verification; creating a fresh Web App deployment."
  npx -y "$CLASP" create-deployment \
    --description "Hunter V2 monthly bridge fresh ${MERGED_MAIN_SHA}"
  DEPLOYMENTS="$(npx -y "$CLASP" list-deployments)"
  printf '%s\n' "$DEPLOYMENTS"
  DEPLOYMENT_ID="$(printf '%s\n' "$DEPLOYMENTS" | \
    python "$ROOT/cloudrun/resolve-clasp-deployment.py")"
  BRIDGE_URL="https://script.google.com/macros/s/${DEPLOYMENT_ID}/exec"
  export BRIDGE_URL
  if ! bridge_ready; then
    echo "BRIDGE_WEBAPP_GET_POST_VALIDATION_FAILED:$BRIDGE_URL" >&2
    exit 1
  fi
fi

DAILY_TRIGGER_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" \
  python "$ROOT/cloudrun/apps-script-post.py" --op enforce_daily_triggers --raw)"
printf '%s\n' "$DAILY_TRIGGER_JSON" | python -c '
import json,sys
d=json.load(sys.stdin)
daily=d.get("daily") or {}
counts=daily.get("counts") or {}
tz=daily.get("timeZone")
ok=(d.get("ok") is True and
    counts.get("dailyUS")==1 and counts.get("dailyHK")==1 and
    counts.get("hunterDailyWatchdog")==1 and
    tz in ("Asia/Kuala_Lumpur","Asia/Singapore"))
if not ok:
    raise SystemExit("DAILY_TRIGGER_VERIFY_FAILED:" + json.dumps(d,sort_keys=True))
'
echo "DAILY_TRIGGER_ENFORCEMENT=PASS"

daily_trigger_readback_ok() {
  local primary fallback
  primary="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" \
    python "$ROOT/cloudrun/apps-script-post.py" --op daily_trigger_status --raw)" || return 1
  if printf '%s\n' "$primary" | python -c '
import json,sys
d=json.load(sys.stdin)
daily=d.get("daily") or {}
counts=daily.get("counts") or {}
raise SystemExit(0 if (d.get("ok") is True and
                       counts.get("dailyUS")==1 and
                       counts.get("dailyHK")==1 and
                       counts.get("hunterDailyWatchdog")==1) else 1)
'; then
    DAILY_TRIGGER_STATUS="$primary"
    return 0
  fi

  # Apps Script version propagation can briefly route one request to the
  # previous deployment. topology_status exists in both generations, so use it
  # as a compatibility readback, but only accept it when watchdog=1 proves the
  # new Gateway is actually serving.
  fallback="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" \
    python "$ROOT/cloudrun/apps-script-post.py" --op topology_status --raw)" || return 1
  DAILY_TRIGGER_STATUS="$fallback"
  printf '%s\n' "$fallback" | python -c '
import json,sys
d=json.load(sys.stdin)
daily=d.get("daily") or {}
counts=daily.get("counts") or {}
raise SystemExit(0 if (d.get("ok") is True and
                       counts.get("dailyUS")==1 and
                       counts.get("dailyHK")==1 and
                       counts.get("hunterDailyWatchdog")==1) else 1)
'
}

DAILY_TRIGGER_STATUS=""
for attempt in $(seq 1 12); do
  if daily_trigger_readback_ok; then
    echo "DAILY_TRIGGER_READBACK=PASS"
    break
  fi
  if [[ "$attempt" -eq 12 ]]; then
    echo "DAILY_TRIGGER_READBACK_FAILED_AFTER_PROPAGATION_WAIT:$DAILY_TRIGGER_STATUS" >&2
    exit 1
  fi
  sleep 5
done

echo "BRIDGE_DEPLOYMENT=PASS"
echo "BRIDGE_POST=PASS"
unset LEGACY_KEY

# Publish only a deployment URL that passed both GET and authenticated POST.
if [[ "$BRIDGE_URL" != "$SECRET_BRIDGE_URL" ]]; then
  printf '%s' "$BRIDGE_URL" | gcloud secrets versions add "$URL_SECRET" \
    --data-file=- --project "$GCP_PROJECT_ID" >/dev/null
  echo "BRIDGE_URL_SECRET_UPDATED=PASS"
else
  echo "BRIDGE_URL_SECRET_CURRENT=PASS"
fi

if [[ "${SKIP_MONTHLY_DEPLOY:-0}" == "1" ]]; then
  echo "MONTHLY_DEPLOY_SKIPPED=PASS"
  exit 0
fi

cd "$ROOT"
bash cloudrun/deploy-monthly.sh
