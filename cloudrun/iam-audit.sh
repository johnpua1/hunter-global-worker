#!/usr/bin/env bash
set -euo pipefail
set +x

PROJECT_ID="${GCP_PROJECT_ID:-rgs-hunter-global}"
REGION="${GCP_REGION:-us-central1}"
TRIGGER_AUTHORITY="${HUNTER_TRIGGER_AUTHORITY:-MIXED_LOCKED}"
JOBS=(hunter-us-daily hunter-hk-daily hunter-maintenance hunter-monthly-v2)
FAIL=0

[[ "$TRIGGER_AUTHORITY" == "MIXED_LOCKED" ]] || {
  echo "INVALID_TRIGGER_AUTHORITY=$TRIGGER_AUTHORITY"
  exit 2
}

command -v gcloud >/dev/null
command -v python >/dev/null
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null

declare -A EXPECTED_SA=(
  [hunter-us-daily]="hunter-us-daily@${PROJECT_ID}.iam.gserviceaccount.com"
  [hunter-hk-daily]="hunter-hk-daily@${PROJECT_ID}.iam.gserviceaccount.com"
  [hunter-maintenance]="hunter-maintenance@${PROJECT_ID}.iam.gserviceaccount.com"
  [hunter-monthly-v2]="hunter-monthly@${PROJECT_ID}.iam.gserviceaccount.com"
)
declare -A JOB_SA

extract_service_account() {
  python -c '
import json, sys
doc=json.load(sys.stdin)
try:
    value=doc["spec"]["template"]["spec"]["template"]["spec"]["serviceAccountName"]
except (KeyError, TypeError):
    raise SystemExit(1)
if not isinstance(value,str) or not value:
    raise SystemExit(1)
print(value)
'
}

for job in "${JOBS[@]}"; do
  raw="$(gcloud run jobs describe "$job" --project "$PROJECT_ID" --region "$REGION" --format=json 2>/dev/null || true)"
  if [[ -z "$raw" ]]; then
    echo "FAIL job=$job reason=JOB_MISSING"
    FAIL=1
    continue
  fi
  sa="$(printf '%s' "$raw" | extract_service_account 2>/dev/null || true)"
  if [[ -z "$sa" ]]; then
    echo "FAIL job=$job reason=SERVICE_ACCOUNT_FIELD_MISSING"
    FAIL=1
    continue
  fi
  JOB_SA["$job"]="$sa"
  echo "JOB job=$job service_account=$sa"
  if [[ "$sa" != "${EXPECTED_SA[$job]}" ]]; then
    echo "FAIL job=$job reason=SERVICE_ACCOUNT_DRIFT expected=${EXPECTED_SA[$job]} got=$sa"
    FAIL=1
  fi
done

mapfile -t RUNTIME_SAS < <(printf '%s\n' "${JOB_SA[@]:-}" | sed '/^$/d' | sort -u)
for sa in "${RUNTIME_SAS[@]}"; do
  echo "PROJECT_ROLES service_account=$sa"
  mapfile -t roles < <(gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter="bindings.members:serviceAccount:${sa}" \
    --format='value(bindings.role)' | sort -u)
  if (("${#roles[@]}"==0)); then
    echo "  (none)"
  fi
  for role in "${roles[@]}"; do
    echo "  $role"
    case "$role" in
      roles/owner|roles/editor|roles/viewer|roles/run.admin|roles/run.developer|roles/run.invoker|\
      roles/secretmanager.admin|roles/secretmanager.secretAccessor|roles/artifactregistry.admin|\
      roles/storage.admin|roles/iam.serviceAccountAdmin|roles/iam.serviceAccountTokenCreator)
        echo "FAIL service_account=$sa reason=OVERBROAD_PROJECT_ROLE role=$role"
        FAIL=1
        ;;
    esac
  done
done

SECRETS=(
  APPS_SCRIPT_WEBAPP_URL
  APPS_SCRIPT_SHARED_KEY
  APPS_SCRIPT_SHARED_KEY_US
  APPS_SCRIPT_SHARED_KEY_HK
  APPS_SCRIPT_SHARED_KEY_MAINT
  APPS_SCRIPT_SHARED_KEY_MONTH
)
for secret in "${SECRETS[@]}"; do
  if gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "SECRET_IAM secret=$secret"
    gcloud secrets get-iam-policy "$secret" --project "$PROJECT_ID" \
      --flatten='bindings[].members' \
      --format='table(bindings.role,bindings.members)' || true
  fi
done

for job in "${JOBS[@]}"; do
  echo "JOB_IAM job=$job"
  gcloud run jobs get-iam-policy "$job" --project "$PROJECT_ID" --region "$REGION" \
    --flatten='bindings[].members' --format='table(bindings.role,bindings.members)' || true
done

SCHEDULERS_JSON="$(gcloud scheduler jobs list --project "$PROJECT_ID" --location "$REGION" \
  --filter='name:(hunter-us-daily hunter-hk-daily hunter-maintenance hunter-monthly-v2)' \
  --format=json 2>/dev/null || printf '[]')"

while IFS=$'\t' read -r name state; do
  [[ -n "$name" ]] || continue
  echo "SCHEDULER $name $state"
  case "$name" in
    hunter-maintenance)
      [[ "$state" == "ENABLED" ]] && MAINT_SCHED_ENABLED=1
      ;;
    hunter-us-daily|hunter-hk-daily|hunter-monthly-v2)
      if [[ "$state" == "ENABLED" ]]; then
        echo "FAIL reason=DUAL_TRIGGER_RISK scheduler=$name state=$state"
        FAIL=1
      fi
      ;;
  esac
done < <(SCHEDULERS_JSON="$SCHEDULERS_JSON" python -c '
import json, os
for row in json.loads(os.environ["SCHEDULERS_JSON"]):
    name=str(row.get("name","")).rsplit("/",1)[-1]
    state=str(row.get("state",""))
    print(name+"\t"+state)
')

MAINT_SCHED_ENABLED="${MAINT_SCHED_ENABLED:-0}"
if [[ "$MAINT_SCHED_ENABLED" != "1" ]]; then
  echo "FAIL reason=MAINTENANCE_SCHEDULER_NOT_ENABLED"
  FAIL=1
fi

if ((FAIL)); then
  echo "IAM_AUDIT=FAIL"
  exit 1
fi
echo "IAM_AUDIT=PASS"
