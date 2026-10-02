#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GCP_PROJECT_ID:?Set the existing Google Cloud project ID}"
: "${MERGED_MAIN_SHA:?Set the verified merged main SHA}"
REGION="${GCP_REGION:-us-central1}"
JOB="hunter-quarterly-v2"
SA_ID="hunter-quarterly"
SA="${SA_ID}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
URL_SECRET="APPS_SCRIPT_WEBAPP_URL"
LEGACY_KEY_SECRET="APPS_SCRIPT_SHARED_KEY"
QUARTER_KEY_SECRET="APPS_SCRIPT_SHARED_KEY_QUARTER"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$MERGED_MAIN_SHA"

gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectId)' >/dev/null
gcloud artifacts repositories describe hunter-worker --location "$REGION"   --project "$GCP_PROJECT_ID" >/dev/null

for secret in "$URL_SECRET" "$LEGACY_KEY_SECRET"; do
  gcloud secrets describe "$secret" --project "$GCP_PROJECT_ID" >/dev/null
  gcloud secrets versions list "$secret" --project "$GCP_PROJECT_ID"     --filter='state=ENABLED' --format='value(name)' | grep -q .
done

# Bootstrap one path-scoped quarterly Bridge credential through the already
# deployed Bridge. The legacy credential is held only in shell variables and
# never printed or passed on a process command line.
BRIDGE_URL="$(gcloud secrets versions access latest --secret "$URL_SECRET" --project "$GCP_PROJECT_ID")"
LEGACY_KEY="$(gcloud secrets versions access latest --secret "$LEGACY_KEY_SECRET" --project "$GCP_PROJECT_ID")"
BOOTSTRAP_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python - <<'PY'
import json, os, urllib.request
body=json.dumps({"op":"bootstrap_quarter_key","key":os.environ["LEGACY_KEY"]}).encode()
req=urllib.request.Request(os.environ["BRIDGE_URL"],data=body,
    headers={"Content-Type":"application/json"},method="POST")
with urllib.request.urlopen(req,timeout=60) as r:
    data=json.load(r)
if not data.get("ok") or not data.get("key"):
    raise SystemExit("Bridge quarterly bootstrap unavailable. Deploy the current bridge/Gateway.gs Web App first.")
print(json.dumps({"key":data["key"]},separators=(",",":")))
PY
)"
QUARTER_KEY="$(BOOTSTRAP_JSON="$BOOTSTRAP_JSON" python - <<'PY'
import json,os
print(json.loads(os.environ["BOOTSTRAP_JSON"])["key"])
PY
)"
unset BOOTSTRAP_JSON LEGACY_KEY

if ! gcloud secrets describe "$QUARTER_KEY_SECRET" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud secrets create "$QUARTER_KEY_SECRET" --replication-policy=automatic --project "$GCP_PROJECT_ID" >/dev/null
fi
printf '%s' "$QUARTER_KEY" | gcloud secrets versions add "$QUARTER_KEY_SECRET"   --data-file=- --project "$GCP_PROJECT_ID" >/dev/null
unset QUARTER_KEY

if ! gcloud iam service-accounts describe "$SA" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_ID"     --display-name='Hunter V2 quarterly recertification' --project "$GCP_PROJECT_ID" >/dev/null
fi
for secret in "$URL_SECRET" "$QUARTER_KEY_SECRET"; do
  gcloud secrets add-iam-policy-binding "$secret" --project "$GCP_PROJECT_ID"     --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
done

IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/hunter-worker/runner:${MERGED_MAIN_SHA}"
gcloud builds submit "$ROOT" --tag "$IMAGE" --project "$GCP_PROJECT_ID" --region "$REGION"

ACTION=create
if gcloud run jobs describe "$JOB" --region "$REGION" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  ACTION=update
fi
gcloud run jobs "$ACTION" "$JOB"   --image "$IMAGE" --region "$REGION" --project "$GCP_PROJECT_ID"   --service-account "$SA" --cpu 2 --memory 8Gi --tasks 1   --task-timeout 60m --max-retries 0   --set-env-vars 'HUNTER_ACTIONS_CUTOVER=CONFIRMED,FETCH_WORKERS=10'   --set-secrets "APPS_SCRIPT_WEBAPP_URL=${URL_SECRET}:latest,APPS_SCRIPT_SHARED_KEY=${QUARTER_KEY_SECRET}:latest"   --args='--mode,quarterly'

# Immediate real-data regression. This execution overrides args only and leaves
# the production job definition unchanged. ACTIVE_POINTER is committed by the
# quarterly worker only after all G178 checks pass.
gcloud run jobs execute "$JOB" --region "$REGION" --project "$GCP_PROJECT_ID"   --args='--mode,quarterly,--snapshot,SNAPSHOT_2026-10-01,--validation'   --task-timeout=60m --wait

# Install the next one-shot Apps Script trigger only after validation succeeds.
LEGACY_KEY="$(gcloud secrets versions access latest --secret "$LEGACY_KEY_SECRET" --project "$GCP_PROJECT_ID")"
TRIGGER_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python - <<'PY'
import json, os, urllib.request
body=json.dumps({"op":"install_quarter_trigger","key":os.environ["LEGACY_KEY"]}).encode()
req=urllib.request.Request(os.environ["BRIDGE_URL"],data=body,
    headers={"Content-Type":"application/json"},method="POST")
with urllib.request.urlopen(req,timeout=60) as r:
    data=json.load(r)
if not data.get("ok"):
    raise SystemExit("Quarter trigger install failed: "+str(data.get("error")))
print(json.dumps(data,separators=(",",":")))
PY
)"
STATUS_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python - <<'PY'
import json, os, urllib.request
body=json.dumps({"op":"quarter_status","key":os.environ["LEGACY_KEY"]}).encode()
req=urllib.request.Request(os.environ["BRIDGE_URL"],data=body,
    headers={"Content-Type":"application/json"},method="POST")
with urllib.request.urlopen(req,timeout=60) as r:
    data=json.load(r)
if not data.get("ok"):
    raise SystemExit("Quarter status failed: "+str(data.get("error")))
print(json.dumps(data,separators=(",",":")))
PY
)"
unset LEGACY_KEY BRIDGE_URL
printf 'VALIDATION=PASS\nJOB=%s\nTRIGGER=%s\nSTATUS=%s\n' "$JOB" "$TRIGGER_JSON" "$STATUS_JSON"
