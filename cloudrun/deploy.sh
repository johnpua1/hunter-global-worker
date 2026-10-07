#!/usr/bin/env bash
set -euo pipefail
set +x

# Run in an authenticated Google Cloud Shell checkout of the released main.
# Never pass the Bridge key on the command line or place it in this repository.
: "${GCP_PROJECT_ID:?Set the existing standard Google Cloud project ID}"
: "${MERGED_MAIN_SHA:?Set the verified merged main SHA}"
REGION=us-central1
IAM_MODE="${HUNTER_IAM_MODE:-scoped}"
case "$IAM_MODE" in
  scoped) ;;
  *) echo "HUNTER_IAM_MODE is locked to scoped; legacy shared runtime identity is retired" >&2; exit 2 ;;
esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$MERGED_MAIN_SHA"
gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectId)' >/dev/null
case "$(gcloud beta billing projects describe "$GCP_PROJECT_ID" --format='value(billingEnabled)' | tr '[:upper:]' '[:lower:]')" in
  true) ;;
  *) echo 'Project billing must be enabled before deployment.' >&2; exit 1 ;;
esac

if ! gcloud artifacts repositories describe hunter-worker --location "$REGION" \
    --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
  echo 'Existing hunter-worker Artifact Registry is required.' >&2; exit 1
fi
gcloud artifacts repositories set-cleanup-policies hunter-worker \
  --location "$REGION" --project "$GCP_PROJECT_ID" \
  --policy="$ROOT/cloudrun/artifact-cleanup.json" --no-dry-run

LEGACY_SA="hunter-jobs@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
US_SA="hunter-us-daily@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
HK_SA="hunter-hk-daily@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
MAINT_SA="hunter-maintenance@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
URL_SECRET="APPS_SCRIPT_WEBAPP_URL"
LEGACY_KEY_SECRET="APPS_SCRIPT_SHARED_KEY"
US_KEY_SECRET="APPS_SCRIPT_SHARED_KEY_US"
HK_KEY_SECRET="APPS_SCRIPT_SHARED_KEY_HK"
MAINT_KEY_SECRET="APPS_SCRIPT_SHARED_KEY_MAINT"

require_sa() {
  local sa="$1"
  gcloud iam service-accounts describe "$sa" --project "$GCP_PROJECT_ID" >/dev/null 2>&1 || {
    echo "Missing service account: $sa" >&2; exit 1;
  }
}
require_secret() {
  local name="$1"
  gcloud secrets describe "$name" --project "$GCP_PROJECT_ID" >/dev/null 2>&1 || {
    echo "Missing Secret Manager entry: $name" >&2; exit 1;
  }
  gcloud secrets versions list "$name" --project "$GCP_PROJECT_ID" \
    --filter='state=ENABLED' --format='value(name)' | grep -q . || {
      echo "No enabled version for secret: $name" >&2; exit 1;
    }
}
grant_secret() {
  local name="$1" sa="$2"
  gcloud secrets add-iam-policy-binding "$name" --project "$GCP_PROJECT_ID" \
    --member="serviceAccount:${sa}" --role=roles/secretmanager.secretAccessor >/dev/null
}
assert_no_broad_project_roles() {
  local sa="$1" role
  while IFS= read -r role; do
    case "$role" in
      roles/owner|roles/editor|roles/viewer|roles/run.admin|roles/run.developer|\
      roles/secretmanager.admin|roles/artifactregistry.admin|roles/storage.admin)
        echo "Overbroad project-level role on ${sa}: ${role}" >&2
        exit 1
        ;;
    esac
  done < <(gcloud projects get-iam-policy "$GCP_PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter="bindings.members:serviceAccount:${sa}" \
    --format='value(bindings.role)')
}

require_secret "$URL_SECRET"
if [[ "$IAM_MODE" == legacy ]]; then
  require_sa "$LEGACY_SA"
  require_secret "$LEGACY_KEY_SECRET"
  grant_secret "$URL_SECRET" "$LEGACY_SA"
  grant_secret "$LEGACY_KEY_SECRET" "$LEGACY_SA"
  US_RUNTIME_SA="$LEGACY_SA"
  HK_RUNTIME_SA="$LEGACY_SA"
  MAINT_RUNTIME_SA="$LEGACY_SA"
  US_RUNTIME_KEY="$LEGACY_KEY_SECRET"
  HK_RUNTIME_KEY="$LEGACY_KEY_SECRET"
  MAINT_RUNTIME_KEY="$LEGACY_KEY_SECRET"
else
  for sa in "$US_SA" "$HK_SA" "$MAINT_SA"; do
    require_sa "$sa"
    assert_no_broad_project_roles "$sa"
    grant_secret "$URL_SECRET" "$sa"
  done
  require_secret "$US_KEY_SECRET"
  require_secret "$HK_KEY_SECRET"
  require_secret "$MAINT_KEY_SECRET"
  grant_secret "$US_KEY_SECRET" "$US_SA"
  grant_secret "$HK_KEY_SECRET" "$HK_SA"
  grant_secret "$MAINT_KEY_SECRET" "$MAINT_SA"
  US_RUNTIME_SA="$US_SA"
  HK_RUNTIME_SA="$HK_SA"
  MAINT_RUNTIME_SA="$MAINT_SA"
  US_RUNTIME_KEY="$US_KEY_SECRET"
  HK_RUNTIME_KEY="$HK_KEY_SECRET"
  MAINT_RUNTIME_KEY="$MAINT_KEY_SECRET"
fi

IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/hunter-worker/runner:${MERGED_MAIN_SHA}"
gcloud builds submit "$ROOT" --tag "$IMAGE" --project "$GCP_PROJECT_ID" --region "$REGION"

deploy_job() {
  local name="$1" runtime_sa="$2" key_secret="$3"; shift 3
  local action=create
  local task_timeout=60m max_retries=1
  # DAILY needs an uninterrupted window for backfill plus derived results.
  # Same 120-minute maximum task-attempt budget; maintenance is unchanged.
  if [[ "$name" == hunter-us-daily || "$name" == hunter-hk-daily ]]; then
    task_timeout=120m
    max_retries=0
  fi
  if gcloud run jobs describe "$name" --region "$REGION" --project "$GCP_PROJECT_ID" >/dev/null 2>&1; then
    action=update
  fi
  gcloud run jobs "$action" "$name" --image "$IMAGE" --region "$REGION" \
    --project "$GCP_PROJECT_ID" --service-account "$runtime_sa" \
    --cpu 1 --memory 512Mi --tasks 1 --task-timeout "$task_timeout" --max-retries "$max_retries" \
    --set-env-vars 'HUNTER_ACTIONS_CUTOVER=CONFIRMED,FETCH_WORKERS=10' \
    --set-secrets "APPS_SCRIPT_WEBAPP_URL=${URL_SECRET}:latest,APPS_SCRIPT_SHARED_KEY=${key_secret}:latest" \
    "$@"
}
deploy_job hunter-us-daily "$US_RUNTIME_SA" "$US_RUNTIME_KEY" --args='--mode,auto,--market,US'
deploy_job hunter-hk-daily "$HK_RUNTIME_SA" "$HK_RUNTIME_KEY" --args='--mode,auto,--market,HK'
deploy_job hunter-maintenance "$MAINT_RUNTIME_SA" "$MAINT_RUNTIME_KEY" --command=python --args=/app/maintenance.py

IMAGE_BASE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/hunter-worker/runner"
for stable_tag in prod-us prod-hk prod-maint; do
  gcloud artifacts docker tags add "$IMAGE" "${IMAGE_BASE}:${stable_tag}" --project "$GCP_PROJECT_ID" --quiet >/dev/null
done

printf 'STAGED_IMAGE=%s\nIAM_MODE=%s\nPRODUCTION_TAGS=prod-us,prod-hk,prod-maint\nSCHEDULERS=NOT_CREATED\n' "$IMAGE" "$IAM_MODE"
