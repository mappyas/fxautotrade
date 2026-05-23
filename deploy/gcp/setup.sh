#!/bin/bash
# GCP Compute Engine VM セットアップスクリプト
# 初回のみ実行する

set -e

# Python & git インストール
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3.11-venv git

# リポジトリをクローン
cd /opt
sudo git clone https://github.com/mappyas/fxautotrade.git fxautobuy
sudo chown -R $USER:$USER /opt/fxautobuy
cd /opt/fxautobuy

# venv 作成 & 依存関係インストール
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# 環境変数ファイルを作成（値は後で編集）
cat > /opt/fxautobuy/.env << 'EOF'
AI_PROVIDER=claude
DATA_SOURCE=yfinance
ANTHROPIC_API_KEY=your_key_here
DISCORD_WEBHOOK_URL=your_webhook_here
EOF

# data ディレクトリ作成
mkdir -p /opt/fxautobuy/data

# cron 設定（JST → UTC 変換）
# 08:30 JST = 23:30 UTC (前日)
# 15:30 JST = 06:30 UTC
# 20:30 JST = 11:30 UTC
# 23:00 JST = 14:00 UTC
# plan_job cron（JST → UTC）
# 08:30 JST = 23:30 UTC / 15:30 JST = 06:30 UTC / 20:30 JST = 11:30 UTC / 23:00 JST = 14:00 UTC
PLAN_CMD="/opt/fxautobuy/venv/bin/python3 /opt/fxautobuy/scripts/plan_job.py >> /opt/fxautobuy/data/plan_job.log 2>&1"
(crontab -l 2>/dev/null; echo "30 23 * * * $PLAN_CMD") | crontab -
(crontab -l 2>/dev/null; echo "30  6 * * * $PLAN_CMD") | crontab -
(crontab -l 2>/dev/null; echo "30 11 * * * $PLAN_CMD") | crontab -
(crontab -l 2>/dev/null; echo " 0 14 * * * $PLAN_CMD") | crontab -

# check_job cron（23:30 JST = 14:30 UTC）
CHECK_CMD="/opt/fxautobuy/venv/bin/python3 /opt/fxautobuy/scripts/check_job.py >> /opt/fxautobuy/data/check_job.log 2>&1"
(crontab -l 2>/dev/null; echo "30 14 * * * $CHECK_CMD") | crontab -

echo "=== セットアップ完了 ==="
echo "次のステップ: nano /opt/fxautobuy/.env でAPIキーを設定してください"
echo ""
echo "設定済みcron:"
crontab -l
