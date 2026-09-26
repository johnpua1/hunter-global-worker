#!/usr/bin/env bash
set -euo pipefail
: "${GCP_PROJECT_ID:?Set project ID}"
: "${BILLING_ACCOUNT_ID:?Set billing account ID}"
: "${BUDGET_AMOUNT:?Set account-currency equivalent of US$1, e.g. 1USD if billed in USD}"
gcloud services enable billingbudgets.googleapis.com --project "$GCP_PROJECT_ID"
if gcloud billing budgets list --billing-account "$BILLING_ACCOUNT_ID" \
    --format='value(displayName)' | grep -Fxq "Hunter ${GCP_PROJECT_ID} low-cost alert"; then
  echo 'Existing Hunter budget alert preserved'
else
  gcloud billing budgets create --billing-account "$BILLING_ACCOUNT_ID" \
    --display-name="Hunter ${GCP_PROJECT_ID} low-cost alert" \
    --budget-amount="$BUDGET_AMOUNT" --threshold-rule=percent=1.0 \
    --filter-projects="projects/${GCP_PROJECT_ID}"
fi
echo 'Budget alert warns; it does not stop billing or executions.'
