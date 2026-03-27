FROM python:3.12-slim

WORKDIR /app

# 依存パッケージのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコードのコピー
COPY src/ ./src/

# 実行
CMD ["python", "-m", "src.main"]
