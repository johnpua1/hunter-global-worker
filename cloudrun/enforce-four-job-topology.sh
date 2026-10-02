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

EXPECTED_JOBS=(hunter-us-daily hunter-hk-daily hunter-maintenance hunter-monthly-v2)

# Known retired legacy job is safe to remove automatically before the exact-four check.
if gcloud run jobs describe hunter-quarterly-v2 --project "$GCP_PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
  gcloud run jobs delete hunter-quarterly-v2 --project "$GCP_PROJECT_ID" --region "$REGION" --quiet >/dev/null
fi

for job in "${EXPECTED_JOBS[@]}"; do
  gcloud run jobs describe "$job" --project "$GCP_PROJECT_ID" --region "$REGION" >/dev/null
done

mapfile -t LIVE_HUNTER_JOBS < <(gcloud run jobs list --project "$GCP_PROJECT_ID" --region "$REGION" \
  --format='value(name)' | sed 's#.*/##' | grep '^hunter-' | sort -u || true)
mapfile -t UNEXPECTED_HUNTER_JOBS < <(comm -23 \
  <(printf '%s\n' "${LIVE_HUNTER_JOBS[@]}" | sort -u) \
  <(printf '%s\n' "${EXPECTED_JOBS[@]}" | sort -u))
if (("${#UNEXPECTED_HUNTER_JOBS[@]}")); then
  printf 'UNEXPECTED_HUNTER_JOB=%s\n' "${UNEXPECTED_HUNTER_JOBS[@]}" >&2
  exit 1
fi

US_SA="hunter-us-daily@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
HK_SA="hunter-hk-daily@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
MAINT_SA="hunter-maintenance@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
MONTH_SA="hunter-monthly@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

for sa in "$US_SA" "$HK_SA" "$MAINT_SA" "$MONTH_SA"; do
  gcloud iam service-accounts describe "$sa" --project "$GCP_PROJECT_ID" >/dev/null
done
for secret in APPS_SCRIPT_WEBAPP_URL APPS_SCRIPT_SHARED_KEY_US APPS_SCRIPT_SHARED_KEY_HK APPS_SCRIPT_SHARED_KEY_MAINT APPS_SCRIPT_SHARED_KEY_MONTH; do
  gcloud secrets describe "$secret" --project "$GCP_PROJECT_ID" >/dev/null
  gcloud secrets versions list "$secret" --project "$GCP_PROJECT_ID" \
    --filter='state=ENABLED' --format='value(name)' | grep -q .
done

# Build once and self-heal each existing job back to its dedicated runtime
# identity + scoped Bridge key. CPU/memory/task timeout/entry args are preserved.
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/hunter-worker/runner:${EXPECTED_SHA}"
gcloud builds submit "$ROOT" --tag "$IMAGE" --project "$GCP_PROJECT_ID" --region "$REGION"

gcloud run jobs update hunter-us-daily --image "$IMAGE" --service-account "$US_SA" \
  --update-secrets "APPS_SCRIPT_WEBAPP_URL=APPS_SCRIPT_WEBAPP_URL:latest,APPS_SCRIPT_SHARED_KEY=APPS_SCRIPT_SHARED_KEY_US:latest" \
  --project "$GCP_PROJECT_ID" --region "$REGION" >/dev/null

gcloud run jobs update hunter-hk-daily --image "$IMAGE" --service-account "$HK_SA" \
  --update-secrets "APPS_SCRIPT_WEBAPP_URL=APPS_SCRIPT_WEBAPP_URL:latest,APPS_SCRIPT_SHARED_KEY=APPS_SCRIPT_SHARED_KEY_HK:latest" \
  --project "$GCP_PROJECT_ID" --region "$REGION" >/dev/null

gcloud run jobs update hunter-maintenance --image "$IMAGE" --service-account "$MAINT_SA" \
  --update-secrets "APPS_SCRIPT_WEBAPP_URL=APPS_SCRIPT_WEBAPP_URL:latest,APPS_SCRIPT_SHARED_KEY=APPS_SCRIPT_SHARED_KEY_MAINT:latest" \
  --project "$GCP_PROJECT_ID" --region "$REGION" >/dev/null

gcloud run jobs update hunter-monthly-v2 --image "$IMAGE" --service-account "$MONTH_SA" \
  --update-secrets "APPS_SCRIPT_WEBAPP_URL=APPS_SCRIPT_WEBAPP_URL:latest,APPS_SCRIPT_SHARED_KEY=APPS_SCRIPT_SHARED_KEY_MONTH:latest" \
  --project "$GCP_PROJECT_ID" --region "$REGION" >/dev/null

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

STATUS_JSON="$STATUS_JSON" EXPECTED_SHA="$EXPECTED_SHA" python - <<'PY'
import json, os
doc=json.loads(os.environ["STATUS_JSON"])
sha=os.environ["EXPECTED_SHA"]
if not doc.get("ok"):
    raise SystemExit("TOPOLOGY_STATUS_BRIDGE_FAIL:"+str(doc.get("error")))
daily=doc["daily"]
monthly=doc["monthly"]
jobs=doc["jobs"]
checks={
  "dailyUS": daily.get("counts",{}).get("dailyUS")==1,
  "dailyHK": daily.get("counts",{}).get("dailyHK")==1,
  "dailyTZ": daily.get("timeZone") in {"Asia/Kuala_Lumpur","Asia/Singapore"},
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
  "USImage": jobs.get("US",{}).get("image","").endswith(":"+sha),
  "HKImage": jobs.get("HK",{}).get("image","").endswith(":"+sha),
  "MAINTImage": jobs.get("MAINT",{}).get("image","").endswith(":"+sha),
  "MONTHImage": jobs.get("MONTH",{}).get("image","").endswith(":"+sha),
}
bad={k:v for k,v in checks.items() if not v}
if bad:
    raise SystemExit("TOPOLOGY_LOCK_MISMATCH:"+json.dumps({"bad":bad,"status":doc},ensure_ascii=False,separators=(",",":")))
print("TOPOLOGY_STATUS=PASS")
PY

HUNTER_TRIGGER_AUTHORITY=MIXED_LOCKED bash cloudrun/iam-audit.sh

printf 'TOPOLOGY_LOCK=PASS\nENFORCE=%s\nSTATUS=%s\n' "$ENFORCE_JSON" "$STATUS_JSON"
