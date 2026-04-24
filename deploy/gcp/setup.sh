#!/bin/bash
# GCP Compute Engine VM セットアップスクリプト
# 初回のみ実行する

set -e

# Python & git インストール
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip git

# リポジトリをクローン
cd /opt
sudo git clone https://github.com/mappyas/fxautotrade.git fxautobuy
sudo chown -R $USER:$USER /opt/fxautobuy
cd fxautobuy

# 依存関係インストール
pip3 install -r requirements.txt

# 環境変数ファイルを作成（値は後で編集）
cat > /opt/fxautobuy/.env << 'EOF'
AI_PROVIDER=claude
DATA_SOURCE=yfinance
ANTHROPIC_API_KEY=your_key_here
DISCORD_WEBHOOK_URL=your_webhook_here
EOF

echo "=== セットアップ完了 ==="
echo "次のステップ: nano /opt/fxautobuy/.env でAPIキーを設定してください"
