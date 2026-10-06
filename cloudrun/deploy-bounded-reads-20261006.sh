#!/usr/bin/env bash
# Run from the exact reviewed checkout. Keep HK's current execution running.
set -Eeuo pipefail
set +x
umask 077
trap 'rc=$?; echo "DEPLOY_STOPPED: line=$LINENO exit=$rc" >&2; exit "$rc"' ERR
SOURCE="${1:?Pass the reviewed commit SHA}"
[[ "$SOURCE" =~ ^[0-9a-f]{40}$ ]]
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test "$(git -C "$ROOT" rev-parse HEAD)" = "$SOURCE"
PROJECT=rgs-hunter-global
REGION=us-central1
BASE="$REGION-docker.pkg.dev/$PROJECT/hunter-worker/runner"
IMAGE="$BASE:$SOURCE"
OLD_IMAGE="$BASE:859323f28a76057b51c5e807feec2db6a9aa3b5f"
OLD_US=hunter-us-daily-l5ppl
WORK="$(mktemp -d /tmp/hunter-bounded-reads.XXXXXXXX)"
gc() { gcloud "$@" --project="$PROJECT" --quiet; }

# Save protected rollback inputs and verify scope before building or updating.
for market in us hk; do
  gc run jobs describe "hunter-$market-daily" --region="$REGION" --format=json > "$WORK/$market.before.json"
done
python3 - "$WORK" "$IMAGE" "$OLD_IMAGE" <<'PY'
import json,pathlib,sys
def containers(doc):
    if isinstance(doc,dict):
        for k,v in doc.items():
            if k=='containers': yield from v
            else: yield from containers(v)
    elif isinstance(doc,list):
        for v in doc: yield from containers(v)
for market in ('us','hk'):
    rows=list(containers(json.loads((pathlib.Path(sys.argv[1])/(market+'.before.json')).read_text())))
    if len(rows)!=1 or rows[0].get('image') not in sys.argv[2:]:
        raise SystemExit('UNEXPECTED_IMAGE:'+market)
    if rows[0].get('args')!=['--mode','auto','--market',market.upper()]:
        raise SystemExit('UNEXPECTED_ARGS:'+market)
print('PREFLIGHT=PASS')
PY
cat > "$WORK/build.json" <<JSON
{"steps":[{"name":"gcr.io/cloud-builders/docker","args":["build","--build-arg","HUNTER_SOURCE_SHA=$SOURCE","-t","$IMAGE","."]}],"images":["$IMAGE"],"timeout":"1800s"}
JSON
gc builds submit "$ROOT" --config="$WORK/build.json" --region="$REGION"
for market in us hk; do
  gc run jobs update "hunter-$market-daily" --region="$REGION" --image="$IMAGE" \
    --update-env-vars="HUNTER_SOURCE_SHA=$SOURCE,DAILY_READ_WORKERS=2,DERIVED_READ_WORKERS=2,HUNTER_BRIDGE_READ_TIMEOUT_SECONDS=90" >/dev/null
  gc run jobs describe "hunter-$market-daily" --region="$REGION" --format=json > "$WORK/$market.after.json"
  python3 - "$WORK/$market.after.json" "$IMAGE" "$SOURCE" <<'PY'
import json,sys
def containers(doc):
    if isinstance(doc,dict):
        for k,v in doc.items():
            if k=='containers': yield from v
            else: yield from containers(v)
    elif isinstance(doc,list):
        for v in doc: yield from containers(v)
rows=list(containers(json.load(open(sys.argv[1]))))
if len(rows)!=1 or rows[0].get('image')!=sys.argv[2]:
    raise SystemExit('IMAGE_READBACK_FAILED')
env={x['name']:x.get('value') for x in rows[0].get('env',[])}
expected={'HUNTER_SOURCE_SHA':sys.argv[3],'DAILY_READ_WORKERS':'2',
          'DERIVED_READ_WORKERS':'2','HUNTER_BRIDGE_READ_TIMEOUT_SECONDS':'90'}
if any(env.get(k)!=v for k,v in expected.items()): raise SystemExit('ENV_READBACK_FAILED')
print('TEMPLATE_VERIFIED='+rows[0]['args'][-1])
PY
  gc artifacts docker tags add "$IMAGE" "$BASE:prod-$market" >/dev/null
done

# Reuse the tested state parser. Pending and retrying executions remain active.
python3 - "$ROOT/cloudrun/recover-hk-timeout-20261006.py" "$OLD_US" <<'PY'
import importlib.util,sys,time
spec=importlib.util.spec_from_file_location('recovery',sys.argv[1])
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
mod.JOB='hunter-us-daily'; mod.OLD=sys.argv[2]
rows=mod.executions()
if any(mod.name(x)==mod.OLD and mod.state(x)=='ACTIVE' for x in rows):
    mod.cancel_old()
rows=mod.executions()
active=[x for x in rows if mod.state(x)=='ACTIVE']
if active:
    print('US_EXISTING_EXECUTIONS='+','.join(mod.name(x) for x in active))
elif rows and mod.state(rows[0])=='SUCCEEDED':
    print('US_ALREADY_SUCCEEDED_NO_RESTART')
else:
    launched=mod.gc('run','jobs','execute',mod.JOB,'--async')
    own=mod.name(launched)
    if not own.startswith(mod.JOB+'-'): raise SystemExit('LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT')
    print('US_STARTED='+own,flush=True)
    active=[x for x in mod.executions() if mod.state(x)=='ACTIVE']
    if any(mod.name(x)!=own for x in active):
        mod.gc('run','jobs','executions','cancel',own,'--async')
        print('CONCURRENT_LAUNCH_OWN_EXECUTION_CANCEL_REQUESTED')
print('HK_CURRENT_EXECUTION=UNCHANGED')
print('DATA_RECOVERY=NOT_YET_VERIFIED')
print('CONFIG_DRIFT_BASELINE=NOT_REENROLLED')
PY
