#!/usr/bin/env bash
set -euo pipefail
set +x

PROJECT_ID="${GCP_PROJECT_ID:-rgs-hunter-global}"
REGION="${GCP_REGION:-us-central1}"
IAM_MODE="${HUNTER_IAM_MODE:-legacy}"
TRIGGER_AUTHORITY="${HUNTER_TRIGGER_AUTHORITY:-APPS_SCRIPT}"
JOBS=(hunter-us-daily hunter-hk-daily hunter-maintenance hunter-monthly-v2)
FAIL=0

case "$IAM_MODE" in legacy|scoped) ;; *) echo "INVALID_IAM_MODE=$IAM_MODE"; exit 2 ;; esac
case "$TRIGGER_AUTHORITY" in APPS_SCRIPT|CLOUD_SCHEDULER) ;; *) echo "INVALID_TRIGGER_AUTHORITY=$TRIGGER_AUTHORITY"; exit 2 ;; esac

command -v gcloud >/dev/null
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null

declare -A JOB_SA
for job in "${JOBS[@]}"; do
  sa="$(gcloud run jobs describe "$job" --project "$PROJECT_ID" --region "$REGION" \
        --format='value(template.template.serviceAccount)' 2>/dev/null || true)"
  if [[ -z "$sa" ]]; then
    echo "FAIL job=$job reason=JOB_OR_SERVICE_ACCOUNT_MISSING"
    FAIL=1
    continue
  fi
  JOB_SA["$job"]="$sa"
  echo "JOB job=$job service_account=$sa"
done

EXPECTED_US="hunter-us-daily@${PROJECT_ID}.iam.gserviceaccount.com"
EXPECTED_HK="hunter-hk-daily@${PROJECT_ID}.iam.gserviceaccount.com"
EXPECTED_MAINT="hunter-maintenance@${PROJECT_ID}.iam.gserviceaccount.com"
EXPECTED_MONTH="hunter-monthly@${PROJECT_ID}.iam.gserviceaccount.com"
if [[ "$IAM_MODE" == scoped ]]; then
  [[ "${JOB_SA[hunter-us-daily]:-}" == "$EXPECTED_US" ]] || { echo "FAIL job=hunter-us-daily reason=SA_NOT_SCOPED"; FAIL=1; }
  [[ "${JOB_SA[hunter-hk-daily]:-}" == "$EXPECTED_HK" ]] || { echo "FAIL job=hunter-hk-daily reason=SA_NOT_SCOPED"; FAIL=1; }
  [[ "${JOB_SA[hunter-maintenance]:-}" == "$EXPECTED_MAINT" ]] || { echo "FAIL job=hunter-maintenance reason=SA_NOT_SCOPED"; FAIL=1; }
  [[ "${JOB_SA[hunter-monthly-v2]:-}" == "$EXPECTED_MONTH" ]] || { echo "FAIL job=hunter-monthly-v2 reason=SA_NOT_SCOPED"; FAIL=1; }
else
  if [[ -n "${JOB_SA[hunter-us-daily]:-}" &&
        "${JOB_SA[hunter-us-daily]:-}" == "${JOB_SA[hunter-hk-daily]:-}" &&
        "${JOB_SA[hunter-us-daily]:-}" == "${JOB_SA[hunter-maintenance]:-}" ]]; then
    echo "WARN shared_runtime_identity=${JOB_SA[hunter-us-daily]}"
  fi
fi

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

SECRETS=(APPS_SCRIPT_WEBAPP_URL APPS_SCRIPT_SHARED_KEY APPS_SCRIPT_SHARED_KEY_US APPS_SCRIPT_SHARED_KEY_HK APPS_SCRIPT_SHARED_KEY_MAINT APPS_SCRIPT_SHARED_KEY_MONTH)
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

mapfile -t SCHEDULERS < <(gcloud scheduler jobs list --project "$PROJECT_ID" --location "$REGION" \
  --filter='name:(hunter-us-daily hunter-hk-daily hunter-maintenance hunter-monthly-v2)' \
  --format='value(name,state)' 2>/dev/null || true)
if (("${#SCHEDULERS[@]}")); then
  printf 'SCHEDULER %s\n' "${SCHEDULERS[@]}"
fi

MAINT_SCHED_ENABLED=0
for row in "${SCHEDULERS[@]}"; do
  name="${row%%
if ((FAIL)); then
  echo "IAM_AUDIT=FAIL"
  exit 1
fi
echo "IAM_AUDIT=PASS"
\t'*}"
  state="${row##*
if ((FAIL)); then
  echo "IAM_AUDIT=FAIL"
  exit 1
fi
echo "IAM_AUDIT=PASS"
\t'}"
  case "$name" in
    *hunter-maintenance)
      [[ "$state" == ENABLED ]] && MAINT_SCHED_ENABLED=1
      ;;
    *hunter-us-daily|*hunter-hk-daily|*hunter-monthly-v2)
      if [[ "$state" == ENABLED ]]; then
        echo "FAIL reason=DUAL_TRIGGER_RISK scheduler=$row"
        FAIL=1
      fi
      ;;
  esac
done
if [[ "$MAINT_SCHED_ENABLED" != "1" ]]; then
  echo "FAIL reason=MAINTENANCE_SCHEDULER_NOT_ENABLED"
  FAIL=1
fi

if ((FAIL)); then
  echo "IAM_AUDIT=FAIL"
  exit 1
fi
echo "IAM_AUDIT=PASS"
