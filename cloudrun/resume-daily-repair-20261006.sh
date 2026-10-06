#!/usr/bin/env bash
# Deploy the tested reader repair; submission is not data-health acceptance.
set -Eeuo pipefail
set +x
umask 077
trap 'rc=$?; echo "REPAIR_STOPPED: line=$LINENO exit=$rc" >&2; exit "$rc"' ERR

PROJECT=rgs-hunter-global
REGION=us-central1
SOURCE=859323f28a76057b51c5e807feec2db6a9aa3b5f
WORK="$(mktemp -d "${TMPDIR:-/tmp}/hunter-deploy.XXXXXXXX")"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/hunter-worker/runner:${SOURCE}"
gc() { gcloud "$@" --project="$PROJECT" --quiet; }

echo "1/5 获取已通过测试的固定版本"
git clone --quiet https://github.com/johnpua1/hunter-global-worker.git "$WORK/repo"
git -C "$WORK/repo" checkout --quiet --detach "$SOURCE"
test "$(git -C "$WORK/repo" rev-parse HEAD)" = "$SOURCE"
for job in hunter-us-daily hunter-hk-daily; do
  gc run jobs describe "$job" --region="$REGION" --format='value(metadata.name)' >/dev/null
done

echo "2/5 构建修复镜像；此阶段可能需要几分钟"
cat > "$WORK/build.json" <<JSON
{"steps":[{"name":"gcr.io/cloud-builders/docker","args":["build","--build-arg","HUNTER_SOURCE_SHA=$SOURCE","-t","$IMAGE","."]}],"images":["$IMAGE"],"timeout":"1800s"}
JSON
gc builds submit "$WORK/repo" --config="$WORK/build.json" --region="$REGION"

echo "3/5 更新 US/HK 镜像并读回"
for job in hunter-us-daily hunter-hk-daily; do
  gc run jobs update "$job" --image="$IMAGE" --region="$REGION" >/dev/null
  gc run jobs describe "$job" --region="$REGION" --format=json > "$WORK/job.json"
  python3 - "$WORK/job.json" "$IMAGE" "$job" <<'PY'
import json,sys
def images(x):
    if isinstance(x,dict):
        for k,v in x.items():
            if k=='image': yield v
            else: yield from images(v)
    elif isinstance(x,list):
        for v in x: yield from images(v)
actual=set(images(json.load(open(sys.argv[1]))))
if actual != {sys.argv[2]}:
    raise SystemExit('IMAGE_READBACK_FAILED:'+sys.argv[3])
print('IMAGE_VERIFIED='+sys.argv[3])
PY
done
for tag in prod-us prod-hk; do
  gc artifacts docker tags add "$IMAGE" "${IMAGE%:*}:$tag" >/dev/null
done

# Do not cancel an in-flight writer or launch a second writer for that market.
active_names() {
  gc run jobs executions list --job="$1" --region="$REGION" --format=json > "$WORK/executions.json"
  python3 - "$WORK/executions.json" <<'PY'
import json,sys
rows=json.load(open(sys.argv[1]))
if not isinstance(rows,list): raise SystemExit('EXECUTION_LIST_INVALID')
for x in rows:
    s=x.get('status',x)
    completed=next((c for c in s.get('conditions',[]) if c.get('type')=='Completed'),{})
    terminal=(bool(s.get('completionTime')) or
              completed.get('status') in ('True','False') or
              completed.get('state') in ('CONDITION_SUCCEEDED','CONDITION_FAILED'))
    if not terminal:
        name=x.get('metadata',{}).get('name') or x.get('name')
        if not name: raise SystemExit('EXECUTION_NAME_MISSING')
        print(name.rsplit('/',1)[-1])
PY
}

echo "4/5 检查在途任务；空闲市场启动一次补跑"
for job in hunter-us-daily hunter-hk-daily; do
  active="$(active_names "$job")"
  if [[ -n "$active" ]]; then
    printf 'EXISTING_EXECUTION=%s:%s\n' "$job" "$active"
    echo '已有任务运行，本轮不重复启动；新镜像将在下一次执行生效。'
  else
    gc run jobs execute "$job" --region="$REGION" --async
  fi
done

echo "5/5 返回状态"
for job in hunter-us-daily hunter-hk-daily; do
  echo "JOB=$job"
  gc run jobs executions list --job="$job" --region="$REGION" --limit=3
done
echo 'DEPLOYMENT=PASS'
echo 'DATA_RECOVERY=NOT_YET_VERIFIED'
echo '请把以上结果发回；仍需核对执行错误、检查点和衍生数据。'
