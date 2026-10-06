#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
: "${MERGED_MAIN_SHA:?Set MERGED_MAIN_SHA}"
REGION="${GCP_REGION:-us-central1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$MERGED_MAIN_SHA"

gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectId)' >/dev/null
gcloud artifacts repositories describe hunter-worker   --location "$REGION" --project "$GCP_PROJECT_ID" >/dev/null

IMAGE_BASE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/hunter-worker/runner"
IMAGE="${IMAGE_BASE}:${MERGED_MAIN_SHA}"

echo "BUILD_CURRENT_IMAGE=$IMAGE"
gcloud builds submit "$ROOT" --tag "$IMAGE"   --project "$GCP_PROJECT_ID" --region "$REGION"

for job in hunter-us-daily hunter-hk-daily hunter-maintenance hunter-monthly-v2; do
  gcloud run jobs describe "$job" --region "$REGION"     --project "$GCP_PROJECT_ID" >/dev/null
  gcloud run jobs update "$job" --image "$IMAGE"     --region "$REGION" --project "$GCP_PROJECT_ID" >/dev/null
  bound="$(gcloud run jobs describe "$job" --region "$REGION"     --project "$GCP_PROJECT_ID"     --format='value(template.template.containers[0].image)')"
  case "$bound" in
    "$IMAGE"|"$IMAGE_BASE"@"sha256:"*) ;;
    *) echo "JOB_IMAGE_READBACK_FAILED:$job:$bound" >&2; exit 1 ;;
  esac
  echo "JOB_IMAGE_BOUND=$job:$bound"
done

for stable_tag in prod-us prod-hk prod-maint prod-monthly; do
  gcloud artifacts docker tags add "$IMAGE" "${IMAGE_BASE}:${stable_tag}"     --project "$GCP_PROJECT_ID" --quiet >/dev/null
done
echo "PRODUCTION_IMAGE_TAGS=PASS"

gcloud artifacts repositories set-cleanup-policies hunter-worker   --location "$REGION" --project "$GCP_PROJECT_ID"   --policy="$ROOT/cloudrun/artifact-cleanup.json" --no-dry-run >/dev/null
echo "PRODUCTION_IMAGE_CLEANUP_GUARD=PASS"

set +e
gcloud run jobs execute hunter-us-daily --region "$REGION"   --project "$GCP_PROJECT_ID" --wait > /tmp/hunter-us-catchup.log 2>&1 &
US_PID=$!
gcloud run jobs execute hunter-hk-daily --region "$REGION"   --project "$GCP_PROJECT_ID" --wait > /tmp/hunter-hk-catchup.log 2>&1 &
HK_PID=$!

wait "$US_PID"; US_RC=$?
wait "$HK_PID"; HK_RC=$?
set -e

cat /tmp/hunter-us-catchup.log
cat /tmp/hunter-hk-catchup.log

if [[ "$US_RC" -ne 0 || "$HK_RC" -ne 0 ]]; then
  echo "CATCHUP_FAILED:US=$US_RC:HK=$HK_RC" >&2
  exit 1
fi

echo "CATCHUP_EXECUTIONS=PASS"
echo "IMAGE_REBIND_AND_CATCHUP=PASS"
