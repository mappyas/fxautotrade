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

register_secret() {
  local SECRET_NAME=$1
  local SECRET_VALUE=$2
  if [ -z "${SECRET_VALUE}" ]; then
    echo "${SECRET_NAME} が空のためスキップします"
    return
  fi
  if gcloud secrets describe "${SECRET_NAME}" --quiet 2>/dev/null; then
    echo "${SECRET_NAME} は既に存在します（バージョンを追加）"
    echo -n "${SECRET_VALUE}" | gcloud secrets versions add "${SECRET_NAME}" --data-file=-
  else
    echo -n "${SECRET_VALUE}" | gcloud secrets create "${SECRET_NAME}" --data-file=-
    echo "${SECRET_NAME} を作成しました"
  fi
}

register_secret "GROQ_API_KEY" "${GROQ_API_KEY}"
register_secret "OANDA_API_KEY" "${OANDA_API_KEY}"

# 4. Dockerイメージをビルド＆プッシュ
echo "[4/6] Dockerイメージをビルド & プッシュ..."
gcloud auth configure-docker ${REGION}-docker.pkg.dev --quiet
docker build -t ${IMAGE}:latest .
docker push ${IMAGE}:latest

# 4.5. Secret Manager へのアクセス権を付与
echo "[4.5/6] Secret Manager アクセス権を付与..."
PROJECT_NUMBER=$(gcloud projects describe ${PROJECT_ID} --format='value(projectNumber)')
SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet

# 5. Cloud Run Job を作成
echo "[5/6] Cloud Run Job を作成..."
gcloud run jobs create ${SERVICE} \
  --image=${IMAGE}:latest \
  --region=${REGION} \
  --set-secrets="GROQ_API_KEY=GROQ_API_KEY:latest" \
  --set-env-vars="^|^DATA_SOURCE=yfinance|AI_PROVIDER=groq|PAPER_TRADE=true|PAIRS=USD_JPY,EUR_USD" \
  --max-retries=1 \
  --task-timeout=300s \
  || gcloud run jobs update ${SERVICE} \
    --image=${IMAGE}:latest \
    --region=${REGION} \
    --set-env-vars="^|^DATA_SOURCE=yfinance|AI_PROVIDER=groq|PAPER_TRADE=true|PAIRS=USD_JPY,EUR_USD"

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
