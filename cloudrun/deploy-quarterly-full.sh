#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GCP_PROJECT_ID:=rgs-hunter-global}"
REGION="${GCP_REGION:-us-central1}"
REPO_URL="https://github.com/johnpua1/hunter-global-worker.git"
CLASP="@google/clasp@3.4.1"
WORK_ROOT="${HOME}/hunter-quarterly-release"
REPO_DIR="${WORK_ROOT}/repo"
SCRIPT_DIR="${WORK_ROOT}/apps-script"
URL_SECRET="APPS_SCRIPT_WEBAPP_URL"

gcloud config set project "$GCP_PROJECT_ID" >/dev/null
gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . || gcloud auth login --no-launch-browser

rm -rf "$WORK_ROOT"
mkdir -p "$WORK_ROOT"
git clone --depth=1 "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"
MERGED_MAIN_SHA="$(git rev-parse HEAD)"
export MERGED_MAIN_SHA GCP_PROJECT_ID GCP_REGION="$REGION"

# clasp OAuth is user-scoped and is the only interactive step. Reuse an
# existing login when available; otherwise print the Google authorization URL
# and accept the returned code in Cloud Shell.
if ! npx -y "$CLASP" show-authorized-user --json >/dev/null 2>&1; then
  npx -y "$CLASP" login --no-localhost
fi

BRIDGE_URL="$(gcloud secrets versions access latest --secret "$URL_SECRET" --project "$GCP_PROJECT_ID")"
DEPLOYMENT_ID="$(printf '%s' "$BRIDGE_URL" | sed -n 's#^https://script.google.com/macros/s/\([^/]*\)/exec$#\1#p')"
test -n "$DEPLOYMENT_ID"

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
cp "$REPO_DIR/bridge/Gateway.gs" "$SCRIPT_DIR/Gateway.gs"
cp "$REPO_DIR/bridge/appsscript.json" "$SCRIPT_DIR/appsscript.json"

# Preserve every other remote project file obtained by clone-script; only the
# two version-controlled bridge files above are replaced.
npx -y "$CLASP" push --force
npx -y "$CLASP" list-deployments | grep -F "$DEPLOYMENT_ID" >/dev/null
npx -y "$CLASP" create-deployment --deploymentId "$DEPLOYMENT_ID" \
  --description "Hunter V2 quarterly bridge ${MERGED_MAIN_SHA}"

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

cd "$REPO_DIR"
bash cloudrun/deploy-quarterly.sh
