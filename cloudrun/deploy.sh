#!/usr/bin/env bash
set -euo pipefail
set +x

# Run in an authenticated Google Cloud Shell checkout of the released main.
# Never pass the Bridge key on the command line or place it in this repository.
: "${GCP_PROJECT_ID:?Set the existing standard Google Cloud project ID}"
: "${MERGED_MAIN_SHA:?Set the verified merged main SHA}"
REGION=us-central1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$MERGED_MAIN_SHA"
gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectId)' >/dev/null
case "$(gcloud beta billing projects describe "$GCP_PROJECT_ID" --format='value(billingEnabled)' | tr '[:upper:]' '[:lower:]')" in
  true) ;;
  *) echo 'Project billing must be enabled before deployment.' >&2; exit 1 ;;
esac

gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com --project "$GCP_PROJECT_ID"

if ! gcloud artifacts repositories describe hunter-worker --location "$REGION" \
    --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create hunter-worker --repository-format=docker \
    --location "$REGION" --project "$GCP_PROJECT_ID"
fi
gcloud artifacts repositories set-cleanup-policies hunter-worker \
  --location "$REGION" --project "$GCP_PROJECT_ID" \
  --policy="$ROOT/cloudrun/artifact-cleanup.json"

SA="hunter-jobs@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create hunter-jobs --project "$GCP_PROJECT_ID"
fi
for name in APPS_SCRIPT_WEBAPP_URL APPS_SCRIPT_SHARED_KEY; do
  if ! gcloud secrets describe "$name" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
    gcloud secrets create "$name" --replication-policy=automatic --project "$GCP_PROJECT_ID"
  fi
  if ! gcloud secrets versions list "$name" --project "$GCP_PROJECT_ID" \
      --filter='state=ENABLED' --format='value(name)' | grep -q .; then
    if [[ "$name" == APPS_SCRIPT_SHARED_KEY ]]; then
      read -r -s -p 'Paste Bridge key privately in Cloud Shell: ' value
      printf '\n'
    else
      read -r -p 'Paste Bridge /exec URL privately in Cloud Shell: ' value
    fi
    test -n "$value"
    printf %s "$value" | gcloud secrets versions add "$name" --data-file=- \
      --project "$GCP_PROJECT_ID" >/dev/null
    unset value
  fi
  gcloud secrets add-iam-policy-binding "$name" --project "$GCP_PROJECT_ID" \
    --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor >/dev/null
done

IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/hunter-worker/runner:${MERGED_MAIN_SHA}"
gcloud builds submit "$ROOT" --tag "$IMAGE" --project "$GCP_PROJECT_ID" --region "$REGION"

deploy_job() {
  local name="$1"; shift
  local action=create
  if gcloud run jobs describe "$name" --region "$REGION" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
    action=update
  fi
  gcloud run jobs "$action" "$name" --image "$IMAGE" --region "$REGION" \
    --project "$GCP_PROJECT_ID" --service-account "$SA" \
    --cpu 1 --memory 512Mi --tasks 1 --task-timeout 60m --max-retries 1 \
    --set-env-vars 'HUNTER_ACTIONS_CUTOVER=CONFIRMED,FETCH_WORKERS=10' \
    --set-secrets 'APPS_SCRIPT_WEBAPP_URL=APPS_SCRIPT_WEBAPP_URL:latest,APPS_SCRIPT_SHARED_KEY=APPS_SCRIPT_SHARED_KEY:latest' \
    "$@"
}
deploy_job hunter-us-daily --args='--mode,auto,--market,US'
deploy_job hunter-hk-daily --args='--mode,auto,--market,HK'
deploy_job hunter-maintenance --command=python --args=/app/maintenance.py
printf 'STAGED_IMAGE=%s\nSCHEDULERS=NOT_CREATED\n' "$IMAGE"
