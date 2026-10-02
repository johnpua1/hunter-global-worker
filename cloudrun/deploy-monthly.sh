#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GCP_PROJECT_ID:?Set the existing Google Cloud project ID}"
: "${MERGED_MAIN_SHA:?Set the verified merged main SHA}"
REGION="${GCP_REGION:-us-central1}"
JOB="hunter-monthly-v2"
SA_ID="hunter-monthly"
SA="${SA_ID}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
URL_SECRET="APPS_SCRIPT_WEBAPP_URL"
LEGACY_KEY_SECRET="APPS_SCRIPT_SHARED_KEY"
MONTH_KEY_SECRET="APPS_SCRIPT_SHARED_KEY_MONTH"
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

# Bootstrap one path-scoped monthly Bridge credential through the already
# deployed Bridge. The legacy credential is held only in shell variables and
# never printed or passed on a process command line.
BRIDGE_URL="$(gcloud secrets versions access latest --secret "$URL_SECRET" --project "$GCP_PROJECT_ID")"
LEGACY_KEY="$(gcloud secrets versions access latest --secret "$LEGACY_KEY_SECRET" --project "$GCP_PROJECT_ID")"
BOOTSTRAP_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python - <<'PY'
import json, os, urllib.request
body=json.dumps({"op":"bootstrap_month_key","key":os.environ["LEGACY_KEY"]}).encode()
req=urllib.request.Request(os.environ["BRIDGE_URL"],data=body,
    headers={"Content-Type":"application/json"},method="POST")
with urllib.request.urlopen(req,timeout=60) as r:
    data=json.load(r)
if not data.get("ok") or not data.get("key"):
    raise SystemExit("Bridge monthly bootstrap unavailable. Deploy the current bridge/Gateway.gs Web App first.")
print(json.dumps({"key":data["key"]},separators=(",",":")))
PY
)"
MONTH_KEY="$(BOOTSTRAP_JSON="$BOOTSTRAP_JSON" python - <<'PY'
import json,os
print(json.loads(os.environ["BOOTSTRAP_JSON"])["key"])
PY
)"
unset BOOTSTRAP_JSON LEGACY_KEY

if ! gcloud secrets describe "$MONTH_KEY_SECRET" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud secrets create "$MONTH_KEY_SECRET" --replication-policy=automatic --project "$GCP_PROJECT_ID" >/dev/null
fi
printf '%s' "$MONTH_KEY" | gcloud secrets versions add "$MONTH_KEY_SECRET"   --data-file=- --project "$GCP_PROJECT_ID" >/dev/null
unset MONTH_KEY

if ! gcloud iam service-accounts describe "$SA" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_ID"     --display-name='Hunter V2 monthly recertification' --project "$GCP_PROJECT_ID" >/dev/null
fi
for secret in "$URL_SECRET" "$MONTH_KEY_SECRET"; do
  gcloud secrets add-iam-policy-binding "$secret" --project "$GCP_PROJECT_ID"     --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
done

IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/hunter-worker/runner:${MERGED_MAIN_SHA}"
gcloud builds submit "$ROOT" --tag "$IMAGE" --project "$GCP_PROJECT_ID" --region "$REGION"

ACTION=create
if gcloud run jobs describe "$JOB" --region "$REGION" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  ACTION=update
fi
gcloud run jobs "$ACTION" "$JOB"   --image "$IMAGE" --region "$REGION" --project "$GCP_PROJECT_ID"   --service-account "$SA" --cpu 2 --memory 8Gi --tasks 1   --task-timeout 60m --max-retries 0   --set-env-vars 'HUNTER_ACTIONS_CUTOVER=CONFIRMED,FETCH_WORKERS=10'   --set-secrets "APPS_SCRIPT_WEBAPP_URL=${URL_SECRET}:latest,APPS_SCRIPT_SHARED_KEY=${MONTH_KEY_SECRET}:latest"   --args='--mode,monthly'

# Immediate real-data regression. This execution overrides args only and leaves
# the production job definition unchanged. ACTIVE_POINTER is committed by the
# monthly worker only after all G178 checks pass.
gcloud run jobs execute "$JOB" --region "$REGION" --project "$GCP_PROJECT_ID"   --args='--mode,monthly,--snapshot,SNAPSHOT_2026-10-01,--validation'   --task-timeout=60m --wait

# Fail closed on the committed pointer before arming any future trigger.
LEGACY_KEY="$(gcloud secrets versions access latest --secret "$LEGACY_KEY_SECRET" --project "$GCP_PROJECT_ID")"
POINTER_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python - <<'PY'
import base64, json, os, urllib.request
body=json.dumps({"op":"read","key":os.environ["LEGACY_KEY"],"path":"ACTIVE_POINTER"}).encode()
req=urllib.request.Request(os.environ["BRIDGE_URL"],data=body,
    headers={"Content-Type":"application/json"},method="POST")
with urllib.request.urlopen(req,timeout=60) as r:
    outer=json.load(r)
if not outer.get("ok") or not outer.get("data_base64"):
    raise SystemExit("ACTIVE_POINTER read failed: "+str(outer.get("error")))
doc=json.loads(base64.b64decode(outer["data_base64"]))
expected={
    "month_file":"MONTH_2026-10",
    "snapshot":"SNAPSHOT_2026-10-01",
    "long_direction_signal":"无合格信号",
    "short_direction_signal":"无合格信号",
    "long_vertical_baseline":"MARKET_DRIFT_ONLY",
    "short_vertical_baseline":"OOS_ONLY",
}
bad={k:{"got":doc.get(k),"expected":v} for k,v in expected.items() if doc.get(k)!=v}
if bad:
    raise SystemExit("ACTIVE_POINTER validation failed: "+json.dumps(bad,ensure_ascii=False))
print(json.dumps(doc,ensure_ascii=False,separators=(",",":")))
PY
)"

# Install the next one-shot Apps Script trigger only after validation and
# ACTIVE_POINTER readback both succeed.
TRIGGER_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python - <<'PY'
import json, os, urllib.request
body=json.dumps({"op":"install_month_trigger","key":os.environ["LEGACY_KEY"]}).encode()
req=urllib.request.Request(os.environ["BRIDGE_URL"],data=body,
    headers={"Content-Type":"application/json"},method="POST")
with urllib.request.urlopen(req,timeout=60) as r:
    data=json.load(r)
if not data.get("ok"):
    raise SystemExit("Month trigger install failed: "+str(data.get("error")))
print(json.dumps(data,separators=(",",":")))
PY
)"
STATUS_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python - <<'PY'
import json, os, urllib.request
body=json.dumps({"op":"month_status","key":os.environ["LEGACY_KEY"]}).encode()
req=urllib.request.Request(os.environ["BRIDGE_URL"],data=body,
    headers={"Content-Type":"application/json"},method="POST")
with urllib.request.urlopen(req,timeout=60) as r:
    data=json.load(r)
if not data.get("ok"):
    raise SystemExit("Month status failed: "+str(data.get("error")))
print(json.dumps(data,separators=(",",":")))
PY
)"
unset LEGACY_KEY BRIDGE_URL

# Retire the superseded quarterly deployment only after the monthly worker,
# validation pointer, and monthly trigger have all passed.
OLD_JOB="hunter-quarterly-v2"
OLD_SA="hunter-quarterly@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
OLD_SECRET="APPS_SCRIPT_SHARED_KEY_QUARTER"
if gcloud run jobs describe "$OLD_JOB" --region "$REGION" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud run jobs delete "$OLD_JOB" --region "$REGION" --project "$GCP_PROJECT_ID" --quiet >/dev/null
fi
if gcloud secrets describe "$OLD_SECRET" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud secrets delete "$OLD_SECRET" --project "$GCP_PROJECT_ID" --quiet >/dev/null
fi
if gcloud iam service-accounts describe "$OLD_SA" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts delete "$OLD_SA" --project "$GCP_PROJECT_ID" --quiet >/dev/null
fi

printf 'VALIDATION=PASS\nJOB=%s\nACTIVE_POINTER=%s\nTRIGGER=%s\nSTATUS=%s\nRETIRED=%s\n' "$JOB" "$POINTER_JSON" "$TRIGGER_JSON" "$STATUS_JSON" "$OLD_JOB"
