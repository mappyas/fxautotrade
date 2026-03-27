#!/bin/bash
# GCPリソースの初期セットアップスクリプト
# 実行前に: gcloud auth login && gcloud config set project YOUR_PROJECT_ID

set -e

PROJECT_ID=$(gcloud config get-value project)
REGION="asia-northeast1"
REPO="fxautobuy"
SERVICE="fxautobuy-job"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}"

echo "=== GCP Setup: Project=${PROJECT_ID} Region=${REGION} ==="

# 1. 必要なAPIを有効化
echo "[1/6] APIを有効化..."
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com

# 2. Artifact Registry リポジトリ作成
echo "[2/6] Artifact Registry リポジトリを作成..."
gcloud artifacts repositories create ${REPO} \
  --repository-format=docker \
  --location=${REGION} \
  --description="FX AutoBuy Docker images" \
  || echo "既に存在します（スキップ）"

# 3. Secret Manager にAPIキーを登録
echo "[3/6] Secret Manager にAPIキーを登録..."
echo "GROQ_API_KEY を入力してください:"
read -s GROQ_KEY
echo -n "${GROQ_KEY}" | gcloud secrets create GROQ_API_KEY --data-file=- \
  || echo -n "${GROQ_KEY}" | gcloud secrets versions add GROQ_API_KEY --data-file=-

echo "OANDA_API_KEY を入力してください（不要なら Enter）:"
read -s OANDA_KEY
if [ -n "${OANDA_KEY}" ]; then
  echo -n "${OANDA_KEY}" | gcloud secrets create OANDA_API_KEY --data-file=- \
    || echo -n "${OANDA_KEY}" | gcloud secrets versions add OANDA_API_KEY --data-file=-
fi

# 4. Dockerイメージをビルド＆プッシュ
echo "[4/6] Dockerイメージをビルド & プッシュ..."
gcloud auth configure-docker ${REGION}-docker.pkg.dev --quiet
docker build -t ${IMAGE}:latest .
docker push ${IMAGE}:latest

# 5. Cloud Run Job を作成
echo "[5/6] Cloud Run Job を作成..."
gcloud run jobs create ${SERVICE} \
  --image=${IMAGE}:latest \
  --region=${REGION} \
  --set-secrets="GROQ_API_KEY=GROQ_API_KEY:latest" \
  --set-env-vars="DATA_SOURCE=yfinance,AI_PROVIDER=groq,PAPER_TRADE=true,PAIRS=USD_JPY,EUR_USD" \
  --max-retries=1 \
  --task-timeout=300s \
  || gcloud run jobs update ${SERVICE} \
    --image=${IMAGE}:latest \
    --region=${REGION}

# 6. Cloud Scheduler で15分ごとに実行
echo "[6/6] Cloud Scheduler を設定..."
JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${SERVICE}:run"
SA_EMAIL="$(gcloud iam service-accounts list --filter='displayName:Compute Engine default' --format='value(email)')"

gcloud scheduler jobs create http fxautobuy-scheduler \
  --location=${REGION} \
  --schedule="*/15 * * * *" \
  --uri="${JOB_URI}" \
  --http-method=POST \
  --oauth-service-account-email="${SA_EMAIL}" \
  || echo "Cloud Scheduler ジョブは既に存在します（スキップ）"

echo ""
echo "=== セットアップ完了 ==="
echo "Cloud Run Job:      https://console.cloud.google.com/run/jobs?project=${PROJECT_ID}"
echo "Cloud Scheduler:    https://console.cloud.google.com/cloudscheduler?project=${PROJECT_ID}"
echo "Secret Manager:     https://console.cloud.google.com/security/secret-manager?project=${PROJECT_ID}"
echo ""
echo "手動実行テスト:"
echo "  gcloud run jobs execute ${SERVICE} --region=${REGION}"
