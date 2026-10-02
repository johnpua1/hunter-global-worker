#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GCP_PROJECT_ID:?Set project ID}"
: "${EXPECTED_SHA:?Set expected released main SHA}"
REGION="${GCP_REGION:-us-central1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"

for job in hunter-us-daily hunter-hk-daily hunter-maintenance hunter-monthly-v2; do
  gcloud run jobs describe "$job" --project "$GCP_PROJECT_ID" --region "$REGION" >/dev/null
done

if gcloud run jobs describe hunter-quarterly-v2 --project "$GCP_PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
  gcloud run jobs delete hunter-quarterly-v2 --project "$GCP_PROJECT_ID" --region "$REGION" --quiet >/dev/null
fi

for forbidden in hunter-us-daily hunter-hk-daily hunter-monthly-v2; do
  state="$(gcloud scheduler jobs describe "$forbidden" --project "$GCP_PROJECT_ID" --location "$REGION" --format='value(state)' 2>/dev/null || true)"
  if [[ "$state" == "ENABLED" ]]; then
    gcloud scheduler jobs pause "$forbidden" --project "$GCP_PROJECT_ID" --location "$REGION" >/dev/null
  fi
done

HUNTER_TRIGGER_AUTHORITY=MAINTENANCE_ONLY bash cloudrun/schedule.sh >/dev/null

BRIDGE_URL="$(gcloud secrets versions access latest --secret APPS_SCRIPT_WEBAPP_URL --project "$GCP_PROJECT_ID")"
LEGACY_KEY="$(gcloud secrets versions access latest --secret APPS_SCRIPT_SHARED_KEY --project "$GCP_PROJECT_ID")"

ENFORCE_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python cloudrun/apps-script-post.py --op enforce_topology_triggers --raw)"
STATUS_JSON="$(BRIDGE_URL="$BRIDGE_URL" LEGACY_KEY="$LEGACY_KEY" python cloudrun/apps-script-post.py --op topology_status --raw)"

STATUS_JSON="$STATUS_JSON" python - <<'PY'
import json, os
doc=json.loads(os.environ["STATUS_JSON"])
if not doc.get("ok"):
    raise SystemExit("TOPOLOGY_STATUS_BRIDGE_FAIL:"+str(doc.get("error")))
daily=doc["daily"]
monthly=doc["monthly"]
jobs=doc["jobs"]
checks={
  "dailyUS": daily.get("counts",{}).get("dailyUS")==1,
  "dailyHK": daily.get("counts",{}).get("dailyHK")==1,
  "dailyTZ": daily.get("timeZone")=="Asia/Kuala_Lumpur",
  "monthlyCount": monthly.get("count")==1,
  "USJob": jobs.get("US",{}).get("job")=="hunter-us-daily",
  "HKJob": jobs.get("HK",{}).get("job")=="hunter-hk-daily",
  "MAINTJob": jobs.get("MAINT",{}).get("job")=="hunter-maintenance",
  "MONTHJob": jobs.get("MONTH",{}).get("job")=="hunter-monthly-v2",
  "USArgs": jobs.get("US",{}).get("args")==["--mode","auto","--market","US"],
  "HKArgs": jobs.get("HK",{}).get("args")==["--mode","auto","--market","HK"],
  "MAINTCommand": jobs.get("MAINT",{}).get("command")==["python"],
  "MAINTArgs": jobs.get("MAINT",{}).get("args")==["/app/maintenance.py"],
  "MONTHArgs": jobs.get("MONTH",{}).get("args")==["--mode","monthly"],
}
bad={k:v for k,v in checks.items() if not v}
if bad:
    raise SystemExit("TOPOLOGY_LOCK_MISMATCH:"+json.dumps({"bad":bad,"status":doc},ensure_ascii=False,separators=(",",":")))
print("TOPOLOGY_STATUS=PASS")
PY

HUNTER_TRIGGER_AUTHORITY=MIXED_LOCKED bash cloudrun/iam-audit.sh

printf 'TOPOLOGY_LOCK=PASS\nENFORCE=%s\nSTATUS=%s\n' "$ENFORCE_JSON" "$STATUS_JSON"
