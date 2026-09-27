#!/usr/bin/env bash
set -euo pipefail
set +x

PROJECT_ID="${GCP_PROJECT_ID:-rgs-hunter-global}"
REGION="${GCP_REGION:-us-central1}"
REPO="${HUNTER_REPO:-johnpua1/hunter-global-worker}"
WORKDIR="${HOME}/hunter-daily-release"

echo "== HUNTER DAILY：只更新 US/HK 两个 Cloud Run Job 的镜像 =="
echo "项目：${PROJECT_ID}"
echo "区域：${REGION}"

rm -rf "${WORKDIR}"
git clone "https://github.com/${REPO}.git" "${WORKDIR}"
cd "${WORKDIR}"
git checkout main
git pull --ff-only origin main
SHA="$(git rev-parse HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/hunter-worker/runner:${SHA}"

echo "源码 SHA：${SHA}"
echo "目标镜像：${IMAGE}"

gcloud config set project "${PROJECT_ID}" >/dev/null

for JOB in hunter-us-daily hunter-hk-daily; do
  echo "确认现有 Job：${JOB}"
  gcloud run jobs describe "${JOB}" --project "${PROJECT_ID}" --region "${REGION}" >/dev/null
done

echo "构建新镜像……"
gcloud builds submit . --tag "${IMAGE}" --project "${PROJECT_ID}" --region "${REGION}"

for JOB in hunter-us-daily hunter-hk-daily; do
  echo "只更新镜像：${JOB}"
  gcloud run jobs update "${JOB}"     --image "${IMAGE}"     --project "${PROJECT_ID}"     --region "${REGION}"     --quiet
done

echo "读回两个 Job 的现行镜像："
for JOB in hunter-us-daily hunter-hk-daily; do
  printf '%s  ' "${JOB}"
  gcloud run jobs describe "${JOB}"     --project "${PROJECT_ID}"     --region "${REGION}"     --format='value(template.template.containers[0].image)'
done

echo "完成：只更新 hunter-us-daily / hunter-hk-daily 镜像；未修改 Scheduler、service account、CPU、memory、args、env、secrets 或 maintenance Job。"
