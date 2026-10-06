#!/usr/bin/env bash
set -euo pipefail
set +x

# Daily control-plane repair/cutover only.
# Reuses the audited Apps Script deployment path but explicitly skips the
# monthly Cloud Run deployment. It does not run US/HK Cloud Run jobs, touch
# Hunter market data, or enable the retired GitHub DAILY safety-net.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SKIP_MONTHLY_DEPLOY=1
exec bash "$ROOT/cloudrun/deploy-monthly-full.sh"
