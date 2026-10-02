#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GCP_PROJECT_ID:=rgs-hunter-global}"
REGION="${GCP_REGION:-us-central1}"
CLASP="@google/clasp@3.4.1"
WORK_ROOT="${HOME}/hunter-monthly-release"
SCRIPT_DIR="${WORK_ROOT}/apps-script"
URL_SECRET="APPS_SCRIPT_WEBAPP_URL"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MERGED_MAIN_SHA="$(git rev-parse HEAD)"
if [[ -n "${EXPECTED_SHA:-}" && "$MERGED_MAIN_SHA" != "$EXPECTED_SHA" ]]; then
  echo "HEAD mismatch: expected $EXPECTED_SHA, got $MERGED_MAIN_SHA" >&2
  exit 1
fi
export MERGED_MAIN_SHA GCP_PROJECT_ID GCP_REGION="$REGION"

gcloud config set project "$GCP_PROJECT_ID" >/dev/null
gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . || gcloud auth login --no-launch-browser

rm -rf "$WORK_ROOT"
mkdir -p "$SCRIPT_DIR"

# clasp OAuth is user-scoped and is the only interactive step. Reuse an
# existing login when available; otherwise print the Google authorization URL
# and accept the returned code in Cloud Shell.
if ! npx -y "$CLASP" show-authorized-user --json >/dev/null 2>&1; then
  npx -y "$CLASP" login --no-localhost
fi

SECRET_BRIDGE_URL="$(gcloud secrets versions access latest --secret "$URL_SECRET" --project "$GCP_PROJECT_ID")"
BRIDGE_URL="$SECRET_BRIDGE_URL"
export BRIDGE_URL
SECRET_DEPLOYMENT_ID="$(printf '%s' "$SECRET_BRIDGE_URL" | sed -n 's#^https://script.google.com/macros/s/\([^/]*\)/exec$#\1#p')"
test -n "$SECRET_DEPLOYMENT_ID"

SCRIPTS="$(npx -y "$CLASP" list-scripts)"
SCRIPT_ID="$(printf '%s\n' "$SCRIPTS" | python -c 'import re,sys
rows=[x.strip() for x in sys.stdin if x.strip()]
hits=[]
for x in rows:
    if x.startswith("HUNTER_GLOBAL_BRIDGE"):
        m=re.search(r"[–-]\s*([^\s]+)\s*$",x)
        if m: hits.append(m.group(1))
if len(hits)!=1:
    raise SystemExit("Expected exactly one HUNTER_GLOBAL_BRIDGE in clasp list-scripts; got %d" % len(hits))
print(hits[0])')"

mkdir -p "$SCRIPT_DIR"
cd "$SCRIPT_DIR"
npx -y "$CLASP" clone-script "$SCRIPT_ID"
test -f .clasp.json

# clasp can clone Apps Script sources as .js while this repository owns the
# replacement as .gs. A directory containing both Gateway.js and Gateway.gs is
# rejected as "Conflicting files found". Normalize any duplicate script
# basenames first, then replace the managed Gateway source with exactly one
# extension.
python "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR" --normalize
rm -f "$SCRIPT_DIR/Gateway.js" "$SCRIPT_DIR/Gateway.ts" "$SCRIPT_DIR/Gateway.gs"
cp "$ROOT/bridge/Gateway.gs" "$SCRIPT_DIR/Gateway.gs"
cp "$ROOT/bridge/appsscript.json" "$SCRIPT_DIR/appsscript.json"

# Preserve unrelated remote files (for example Code.js and KeylessBuild.js),
# but fail closed if any basename still exists with more than one script
# extension before push.
python "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR"
npx -y "$CLASP" push --force

# Resolve the actual versioned deployment from clasp itself. Ignore @HEAD
# (development deployment). Prefer the ID currently stored in Secret Manager
# when it is still present; otherwise take the highest versioned deployment.
DEPLOYMENTS="$(npx -y "$CLASP" list-deployments)"
printf '%s\n' "$DEPLOYMENTS"
DEPLOYMENT_ID="$(printf '%s\n' "$DEPLOYMENTS" | \
  python "$ROOT/cloudrun/resolve-clasp-deployment.py" --preferred "$SECRET_DEPLOYMENT_ID")"
test -n "$DEPLOYMENT_ID"

# clasp 3.x exposes a dedicated update-deployment command. Use the resolved
# versioned deployment directly instead of create-deployment --deploymentId.
npx -y "$CLASP" update-deployment "$DEPLOYMENT_ID" \
  --description "Hunter V2 monthly bridge ${MERGED_MAIN_SHA}"

BRIDGE_URL="https://script.google.com/macros/s/${DEPLOYMENT_ID}/exec"
export BRIDGE_URL

python - <<'PY'
import json, os, time, urllib.request
url=os.environ["BRIDGE_URL"]
last=None
for _ in range(8):
    try:
        with urllib.request.urlopen(url,timeout=30) as r:
            body=json.load(r)
        if body=={"ok":True,"service":"HUNTER_GLOBAL_BRIDGE"}:
            print("BRIDGE_DEPLOYMENT=PASS")
            raise SystemExit(0)
        last=body
    except Exception as exc:
        last=repr(exc)
    time.sleep(3)
raise SystemExit("Bridge health verification failed: "+repr(last))
PY

# If Secret Manager pointed to a stale deployment URL, publish the verified
# live URL as a new secret version only after the health check passes.
if [[ "$BRIDGE_URL" != "$SECRET_BRIDGE_URL" ]]; then
  printf '%s' "$BRIDGE_URL" | gcloud secrets versions add "$URL_SECRET" \
    --data-file=- --project "$GCP_PROJECT_ID" >/dev/null
  echo "BRIDGE_URL_SECRET_UPDATED=PASS"
fi

cd "$ROOT"
bash cloudrun/deploy-monthly.sh
