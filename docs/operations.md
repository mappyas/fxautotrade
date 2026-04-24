# 運用マニュアル

## 構成概要

```
GCP Compute Engine (e2-micro / us-central1-a)
  └── background_worker.py（1分ごとにテクニカルアラートチェック）
        └── 条件成立 → Discord 通知

Streamlit Community Cloud
  └── dashboard.py（手動AI分析・チャート確認）

GitHub Actions + cron-job.org
  └── alert_job.py（バックアップ用、現在は補助的）
```

---

## GCP ワーカー操作

### SSH接続
```bash
gcloud compute ssh fxautobuy-worker --project=project-1d19f399-dcf5-46b3-9ab --zone=us-central1-a
```

### サービス状態確認
```bash
sudo systemctl status fxautobuy-worker
```

### ログ確認（リアルタイム）
```bash
sudo journalctl -u fxautobuy-worker -f
```

### 再起動
```bash
sudo systemctl restart fxautobuy-worker
```

### 停止 / 起動
```bash
sudo systemctl stop fxautobuy-worker
sudo systemctl start fxautobuy-worker
```

---

## コードを更新したとき

VM側でpullして再起動：
```bash
cd /opt/fxautobuy
git pull
sudo systemctl restart fxautobuy-worker
```

---

## APIキーを変更したとき

```bash
nano /opt/fxautobuy/.env
sudo systemctl restart fxautobuy-worker
```

---

## VM自体の停止・起動（GCPコスト節約）

```bash
# 停止（課金停止）
gcloud compute instances stop fxautobuy-worker --zone=us-central1-a --project=project-1d19f399-dcf5-46b3-9ab

# 起動
gcloud compute instances start fxautobuy-worker --zone=us-central1-a --project=project-1d19f399-dcf5-46b3-9ab
```

※ e2-microは無料枠なので通常は停止不要

---

## Discord アラート条件

| 条件 | 通知内容 |
|------|----------|
| RSI < 40 | 🔵 買いチャンス候補 |
| RSI > 60 | 🔴 売りチャンス候補 |
| トレンドUP + RSI ≤ 50 | 🟢 押し目買い候補 |
| トレンドDOWN + RSI ≥ 50 | 🟠 戻り売り候補 |
| SMA5 > SMA20 | 📈 買い方向 |
| SMA5 < SMA20 | 📉 売り方向 |

- 同一ペアは**30分間**再通知しない
- 通知来たら Streamlit ダッシュボードでAI分析を実行して判断

---

## シミュレーター実行（ローカル）

```bash
python scripts/sim_runner.py
```

- 開始時刻まで自動待機
- 結果は `data/sim_results.json` に保存
- スリープ設定をOFFにしておくこと

---

## Streamlit ダッシュボード

**URL**: Streamlit Community Cloud のデプロイURL

- **AI分析実行**: サイドバーの「AI分析を実行」ボタン
- **モード切替**: デイトレ（H1/H4/D）/ スキャル（M15/M30/H1）
- **経済指標**: 「経済指標カレンダー」パネルを展開
