#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GCP_PROJECT_ID:?Set project ID}"
: "${EXPECTED_SHA:?Set expected released main SHA}"
CLASP="@google/clasp@3.4.1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_ROOT="${HOME}/hunter-topology-lock"
SCRIPT_DIR="${WORK_ROOT}/apps-script"
cd "$ROOT"

test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"

rm -rf "$WORK_ROOT"
mkdir -p "$SCRIPT_DIR"

if ! npx -y "$CLASP" show-authorized-user --json >/dev/null 2>&1; then
  npx -y "$CLASP" login --no-localhost
fi

BRIDGE_URL="$(gcloud secrets versions access latest --secret APPS_SCRIPT_WEBAPP_URL --project "$GCP_PROJECT_ID")"
PREFERRED_ID="$(printf '%s' "$BRIDGE_URL" | sed -n 's#^https://script.google.com/macros/s/\([^/]*\)/exec$#\1#p')"
test -n "$PREFERRED_ID"

SCRIPTS="$(npx -y "$CLASP" list-scripts)"
SCRIPT_ID="$(printf '%s\n' "$SCRIPTS" | python -c 'import re,sys
hits=[]
for x in sys.stdin:
    x=x.strip()
    if x.startswith("HUNTER_GLOBAL_BRIDGE"):
        m=re.search(r"[–-]\s*([^\s]+)\s*$",x)
        if m: hits.append(m.group(1))
if len(hits)!=1:
    raise SystemExit("Expected exactly one HUNTER_GLOBAL_BRIDGE")
print(hits[0])')"

cd "$SCRIPT_DIR"
npx -y "$CLASP" clone-script "$SCRIPT_ID"
python "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR" --normalize
rm -f Gateway.js Gateway.ts Gateway.gs
cp "$ROOT/bridge/Gateway.gs" Gateway.gs
cp "$ROOT/bridge/appsscript.json" appsscript.json
python "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR"
npx -y "$CLASP" push --force

DEPLOYMENTS="$(npx -y "$CLASP" list-deployments)"
printf '%s\n' "$DEPLOYMENTS"
DEPLOYMENT_ID="$(printf '%s\n' "$DEPLOYMENTS" | python "$ROOT/cloudrun/resolve-clasp-deployment.py" --preferred "$PREFERRED_ID")"
npx -y "$CLASP" update-deployment "$DEPLOYMENT_ID" --description "Hunter four-job topology lock ${EXPECTED_SHA}"

BRIDGE_URL="https://script.google.com/macros/s/${DEPLOYMENT_ID}/exec"
for attempt in $(seq 1 12); do
  if BRIDGE_URL="$BRIDGE_URL" python - <<'PY'
import json, os, urllib.request
with urllib.request.urlopen(os.environ["BRIDGE_URL"], timeout=30) as r:
    body=json.load(r)
raise SystemExit(0 if body=={"ok":True,"service":"HUNTER_GLOBAL_BRIDGE"} else 1)
PY
  then
    break
  fi
  [[ "$attempt" == "12" ]] && { echo "BRIDGE_HEALTH_TIMEOUT" >&2; exit 1; }
  sleep 5
done

cd "$ROOT"
GCP_REGION="${GCP_REGION:-us-central1}" bash cloudrun/enforce-four-job-topology.sh
