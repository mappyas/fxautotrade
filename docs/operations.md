# 運用マニュアル

## 構成概要

```
GCP Compute Engine (e2-micro / us-central1-a)
  ├── background_worker.py（systemd: 1分ごとにテクニカルアラートチェック）
  │     └── 条件成立 + Planのバイアス一致 → Discord 通知
  └── plan_job.py（cron: 1日4回 Planフェーズ実行）
        └── Claude API で取引方針を生成 → data/plan_state.json に保存 → Discord 通知

Streamlit Community Cloud
  └── dashboard.py（手動AI分析・チャート確認）

GitHub Actions + cron-job.org
  └── alert_job.py（バックアップ用、現在は補助的）
```

### Planフェーズ実行タイミング（JST）

| 時刻 | セッション | 役割 |
|------|-----------|------|
| 08:30 | TOKYO | 東京セッション前、当日の大方針を決定 |
| 15:30 | LONDON_OPEN | 東京の結果を受けてロンドン戦略を更新 |
| 20:30 | NY_OPEN | 米指標確認、NY開始前の最終調整 |
| 23:00 | FINAL | 本日の手仕舞い判断・翌日への引き継ぎ |

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

## Plan / Checkジョブのcron設定（既存VMに追加する場合）

SSH接続後、以下を実行：

```bash
# plan_job（1日4回）
PLAN_CMD="/opt/fxautobuy/venv/bin/python3 /opt/fxautobuy/scripts/plan_job.py >> /opt/fxautobuy/data/plan_job.log 2>&1"
(crontab -l 2>/dev/null; echo "30 23 * * * $PLAN_CMD") | crontab -
(crontab -l 2>/dev/null; echo "30  6 * * * $PLAN_CMD") | crontab -
(crontab -l 2>/dev/null; echo "30 11 * * * $PLAN_CMD") | crontab -
(crontab -l 2>/dev/null; echo " 0 14 * * * $PLAN_CMD") | crontab -

# check_job（23:30 JST = 14:30 UTC、FINAL直後）
CHECK_CMD="/opt/fxautobuy/venv/bin/python3 /opt/fxautobuy/scripts/check_job.py >> /opt/fxautobuy/data/check_job.log 2>&1"
(crontab -l 2>/dev/null; echo "30 14 * * * $CHECK_CMD") | crontab -
```

設定確認：
```bash
crontab -l
```

Planログ確認：
```bash
tail -f /opt/fxautobuy/data/plan_job.log
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

テクニカル条件（AIなし）+ Planフィルターの2段階で判定：

| 優先 | 条件 | 方向 |
|------|------|------|
| 1 | MACDヒストグラムがマイナス→プラス + SMA5 > SMA20 | 📈 BUY |
| 1 | MACDヒストグラムがプラス→マイナス + SMA5 < SMA20 | 📉 SELL |
| 2 | RSI < 35 + SMA5 > SMA20 | 📈 BUY候補 |
| 2 | RSI > 65 + SMA5 < SMA20 | 📉 SELL候補 |

**Planフィルター（追加チェック）:**
- `avoid_until` が設定されている時間帯は全エントリー禁止（経済指標前後）
- Planの `bias=SELL` のとき BUY条件を無視（逆も同様）
- トレンドDOWN時はBUY禁止、トレンドUP時はSELL禁止

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
