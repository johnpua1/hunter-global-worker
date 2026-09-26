#!/usr/bin/env bash
set -euo pipefail
set +x
: "${GCP_PROJECT_ID:?Set project ID}"
: "${CLOUD_RUN_US_PASS:?Set YES only after verified US execution}"
: "${CLOUD_RUN_HK_PASS:?Set YES only after verified HK execution}"
test "$CLOUD_RUN_US_PASS" = YES
test "$CLOUD_RUN_HK_PASS" = YES
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
create_schedule hunter-us-daily '30 17 * * 1-5' America/New_York hunter-us-daily
create_schedule hunter-hk-daily '30 17 * * 1-5' Asia/Hong_Kong hunter-hk-daily
create_schedule hunter-maintenance '0 10 * * 6' Asia/Kuala_Lumpur hunter-maintenance
echo 'SCHEDULERS=CREATED; disable GitHub production schedule only after both Cloud Run PASS.'
