#!/usr/bin/env bash
set -euo pipefail
set +x
: "${GCP_PROJECT_ID:?Set project ID}"
if [[ "${HUNTER_TRIGGER_AUTHORITY:-}" != "MAINTENANCE_ONLY" ]]; then
  echo 'Refusing Scheduler mutation: set HUNTER_TRIGGER_AUTHORITY=MAINTENANCE_ONLY explicitly.' >&2
  exit 64
fi
REGION=us-central1
SA="hunter-scheduler@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe "$SA" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  echo 'Existing hunter-scheduler service account is required.' >&2; exit 1
fi

upsert_schedule() {
  local name="$1" cron="$2" timezone="$3" job="$4"
  local uri="https://run.googleapis.com/v2/projects/${GCP_PROJECT_ID}/locations/${REGION}/jobs/${job}:run"

  gcloud run jobs add-iam-policy-binding "$job" --region "$REGION" --project "$GCP_PROJECT_ID" \
    --member="serviceAccount:${SA}" --role=roles/run.invoker >/dev/null

  local action=create
  if gcloud scheduler jobs describe "$name" --location "$REGION" \
      --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
    action=update
  fi
  gcloud scheduler jobs "$action" http "$name" --location "$REGION" \
    --project "$GCP_PROJECT_ID" --schedule "$cron" --time-zone "$timezone" \
    --uri="$uri" --http-method=POST --oauth-service-account-email="$SA" \
    --oauth-token-scope='https://www.googleapis.com/auth/cloud-platform' \
    --max-retry-attempts=0
}

# Production topology lock:
# US/HK DAILY are Apps Script-triggered. Cloud Scheduler is forbidden for those
# jobs because dual trigger authority can create duplicate writers.
for forbidden in hunter-us-daily hunter-hk-daily hunter-monthly-v2; do
  state="$(gcloud scheduler jobs describe "$forbidden" --location "$REGION"     --project "$GCP_PROJECT_ID" --format='value(state)' 2>/dev/null || true)"
  if [[ "$state" == "ENABLED" ]]; then
    echo "DUAL_TRIGGER_RISK:$forbidden:CLOUD_SCHEDULER_ENABLED" >&2
    exit 1
  fi
done

# Maintenance is the only production Cloud Scheduler job.
upsert_schedule hunter-maintenance '0 20 * * *' Asia/Kuala_Lumpur hunter-maintenance

echo 'SCHEDULERS=LOCKED_MAINTENANCE_ONLY'
