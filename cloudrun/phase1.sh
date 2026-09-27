#!/usr/bin/env bash
set -euo pipefail
set +x
: "${GCP_PROJECT_ID:?Set the existing project ID}"
: "${MERGED_MAIN_SHA:?Set the exact merged main SHA}"
REGION=us-central1
test "$GCP_PROJECT_ID" = rgs-hunter-global
test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$MERGED_MAIN_SHA"
TRIGGER_AUTHORITY="${HUNTER_TRIGGER_AUTHORITY:-APPS_SCRIPT}"
case "$TRIGGER_AUTHORITY" in
  APPS_SCRIPT)
    echo 'SCHEDULERS=SKIPPED_TRIGGER_AUTHORITY_APPS_SCRIPT'
    ;;
  CLOUD_SCHEDULER)
    bash cloudrun/schedule.sh
    ;;
  *)
    echo "Unknown HUNTER_TRIGGER_AUTHORITY: $TRIGGER_AUTHORITY" >&2
    exit 2
    ;;
esac

run_market() {
  local market="$1" mode="$2"; shift 2
  local job="hunter-${market,,}-daily"
  gcloud run jobs execute "$job" --project="$GCP_PROJECT_ID" \
    --region="$REGION" --wait --args="--mode,$mode,--market,$market${1:+,$1}"
}

for market in US HK; do
  run_market "$market" bootstrap '--as-of,2026-09-25'
  run_market "$market" universe
  run_market "$market" calendar '--as-of,2026-09-25'
  run_market "$market" options
done
for round in 1 2; do
  gcloud run jobs execute hunter-maintenance --project="$GCP_PROJECT_ID" \
    --region="$REGION" --wait
done
python3 cloudrun/drain_repairs.py
for market in US HK; do
  run_market "$market" analytics '--as-of,2026-09-25'
done
echo 'PHASE1_EXECUTIONS_COMPLETE'
