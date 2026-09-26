#!/usr/bin/env bash
set -euo pipefail
set +x
: "${GCP_PROJECT_ID:?Set project ID}"
REGION=us-central1
SA="hunter-scheduler@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
# Three schedules are the full project allowance for this deployment.
# Abort if another schedule already occupies a slot.
existing="$(gcloud scheduler jobs list --location "$REGION" --project "$GCP_PROJECT_ID" \
  --format='value(name)')"
if [[ -n "$existing" ]]; then
  echo 'Inspect existing Scheduler jobs before creating the three Hunter schedules.' >&2
  exit 1
fi
if ! gcloud iam service-accounts describe "$SA" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create hunter-scheduler --project "$GCP_PROJECT_ID"
fi

create_schedule() {
  local name="$1" cron="$2" timezone="$3" job="$4"
  gcloud run jobs add-iam-policy-binding "$job" --region "$REGION" --project "$GCP_PROJECT_ID" \
    --member="serviceAccount:${SA}" --role=roles/run.invoker >/dev/null
  if gcloud scheduler jobs describe "$name" --location "$REGION" \
      --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
    echo "Existing scheduler $name: inspect before changing it" >&2
    return 1
  fi
  gcloud scheduler jobs create http "$name" --location "$REGION" \
    --project "$GCP_PROJECT_ID" --schedule "$cron" --time-zone "$timezone" \
    --uri="https://run.googleapis.com/v2/projects/${GCP_PROJECT_ID}/locations/${REGION}/jobs/${job}:run" \
    --http-method=POST --oauth-service-account-email="$SA" \
    --oauth-token-scope='https://www.googleapis.com/auth/cloud-platform' \
    --max-retry-attempts=0
}
create_schedule hunter-us-daily '37 8 * * *' Asia/Kuala_Lumpur hunter-us-daily
create_schedule hunter-hk-daily '37 8 * * *' Asia/Kuala_Lumpur hunter-hk-daily
create_schedule hunter-maintenance '0 10 * * 6' Asia/Kuala_Lumpur hunter-maintenance
echo 'SCHEDULERS=CREATED; disable GitHub production schedule after Cloud Run configuration is complete.'
