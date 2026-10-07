#!/usr/bin/env bash
# Update the existing Bridge and daily images, then run guarded recovery/follow-up.
set -euo pipefail
set +x
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPECTED_SHA="${1:?Supply the pinned release SHA}"
test "$(git -C "$ROOT" rev-parse HEAD)" = "$EXPECTED_SHA"
CLASP="@google/clasp@3.4.1"
SCRIPT_DIR="$(mktemp -d /tmp/hunter-large-read-bridge.XXXXXX)"
chmod 700 "$SCRIPT_DIR"

if ! npx -y "$CLASP" show-authorized-user --json >/dev/null 2>&1; then
  npx -y "$CLASP" login --no-localhost
fi
BOUND_DEPLOYMENTS="$(python3 "$ROOT/cloudrun/bridge-release-check-20261008.py" bindings)"
test -n "$BOUND_DEPLOYMENTS"
SCRIPT_ID="$(npx -y "$CLASP" list-scripts | python3 -c 'import re,sys
hits=[]
for line in sys.stdin:
    if line.strip().startswith("HUNTER_GLOBAL_BRIDGE"):
        m=re.search(r"[–-]\s*([^\s]+)\s*$",line.strip())
        if m: hits.append(m.group(1))
if len(hits)!=1: raise SystemExit("Expected exactly one HUNTER_GLOBAL_BRIDGE")
print(hits[0])')"
cd "$SCRIPT_DIR"
npx -y "$CLASP" clone-script "$SCRIPT_ID"
python3 "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR" --normalize
rm -f Gateway.js Gateway.ts Gateway.gs
cp "$ROOT/bridge/Gateway.gs" Gateway.gs
cp "$ROOT/bridge/appsscript.json" appsscript.json
python3 "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR"
python3 "$ROOT/cloudrun/guard-apps-script-governed.py" "$SCRIPT_DIR"
# Verify this exact existing deployment, never create another URL or trigger.
while IFS= read -r DEPLOYMENT_ID; do
  python3 "$ROOT/cloudrun/bridge-release-check-20261008.py" resolve "$SCRIPT_ID" "$DEPLOYMENT_ID"
done <<< "$BOUND_DEPLOYMENTS"
npx -y "$CLASP" push --force
while IFS= read -r DEPLOYMENT_ID; do
  npx -y "$CLASP" update-deployment "$DEPLOYMENT_ID" --description "Hunter durable compressed reads ${EXPECTED_SHA}"
  python3 "$ROOT/cloudrun/bridge-release-check-20261008.py" verify "$SCRIPT_ID" "$DEPLOYMENT_ID"
done <<< "$BOUND_DEPLOYMENTS"
cd "$ROOT"
python3 cloudrun/deploy-large-read-fix-20261008.py "$EXPECTED_SHA"
