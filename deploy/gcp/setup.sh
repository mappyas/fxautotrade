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
DATA_SOURCE=yfinance
DISCORD_WEBHOOK_URL=your_webhook_here
EOF

# data ディレクトリ作成
mkdir -p /opt/fxautobuy/data

# systemd サービス登録
sudo cp /opt/fxautobuy/deploy/gcp/worker.service /etc/systemd/system/fxautobuy.service
sudo systemctl daemon-reload
sudo systemctl enable fxautobuy
sudo systemctl start fxautobuy

echo "=== セットアップ完了 ==="
echo "次のステップ: nano /opt/fxautobuy/.env でAPIキーを設定後、sudo systemctl restart fxautobuy"
echo ""
echo "ステータス確認: sudo systemctl status fxautobuy"
echo "ログ確認:       tail -f /opt/fxautobuy/data/sim_runner.log"
