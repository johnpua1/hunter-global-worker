#!/usr/bin/env bash
set -euo pipefail
set +x
: "${GCP_PROJECT_ID:?Set project ID}"
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

# Malaysia time (MYT, UTC+8).
# US Mon-Fri sessions close the following MYT morning, so run Tue-Sat.
# 07:15 MYT is after the runner's 17:30 America/New_York close gate in both DST and standard time.
upsert_schedule hunter-us-daily '15 7 * * 2-6' Asia/Kuala_Lumpur hunter-us-daily

# HK and Malaysia share UTC+8. Run after the runner's 17:30 Asia/Hong_Kong close gate.
upsert_schedule hunter-hk-daily '45 17 * * 1-5' Asia/Kuala_Lumpur hunter-hk-daily

# Keep maintenance separated from both market writers to reduce write contention.
upsert_schedule hunter-maintenance '0 20 * * *' Asia/Kuala_Lumpur hunter-maintenance

echo 'SCHEDULERS=UPSERTED'
