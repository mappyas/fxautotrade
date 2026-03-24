# FX AutoBuy システム設計書

## 1. システム概要

LLM（Claude API）を用いた FX 自動売買システム。
OANDA v20 API を介してデイトレ・スイングトレードを行う。

- **ブローカー**: OANDA
- **AI エンジン**: Claude API ハイブリッド型（Haiku / Sonnet 自動切替）
- **実行環境**: GCP (Cloud Run + Cloud Scheduler)
- **対象スタイル**: デイトレ（15分〜1時間足）、スイング（4時間〜日足）
- **対象ペア**: USD/JPY、EUR/USD（拡張可能）

---

## 2. ディレクトリ構成

```
fxautobuy/
├── src/
│   ├── data/
│   │   ├── oanda_client.py       # OANDA API ラッパー
│   │   ├── economic_calendar.py  # 経済指標取得
│   │   └── news_fetcher.py       # ニュース・センチメント取得
│   ├── ai/
│   │   ├── analyzer.py           # Claude API 呼び出し・レスポンス解析
│   │   └── prompts.py            # プロンプトテンプレート
│   ├── trading/
│   │   ├── signal.py             # シグナル処理・フィルタリング
│   │   ├── order.py              # 注文実行
│   │   ├── position.py           # ポジション管理
│   │   └── risk.py               # リスク管理
│   ├── db/
│   │   └── repository.py         # Firestore CRUD
│   ├── monitoring/
│   │   └── notifier.py           # アラート通知（Slack/メール）
│   ├── config.py                 # 設定・環境変数
│   └── main.py                   # エントリーポイント
├── tests/
│   ├── test_data.py
│   ├── test_ai.py
│   ├── test_trading.py
│   └── test_risk.py
├── deploy/
│   ├── Dockerfile
│   ├── cloudbuild.yaml
│   └── scheduler.yaml
├── docs/
│   └── architecture.md
├── .env.example
├── requirements.txt
└── pyproject.toml
```

---

## 3. データフロー

```
[Cloud Scheduler] 15分毎にトリガー
        ↓
[main.py] エントリーポイント
        ↓
[Data Layer] 並列でデータ取得
  ├── oanda_client.py  → ローソク足（1H, 4H, 1D）
  ├── economic_calendar.py → 直近・予定の経済指標
  └── news_fetcher.py  → 直近FXニュース
        ↓
[ai/analyzer.py] Claude API に投げる
  ├── Input: ローソク足 + 指標 + ニュース + 現在ポジション
  └── Output: { action, confidence, reasoning, suggested_sl, suggested_tp }
        ↓
[trading/signal.py] シグナル検証
  ├── confidence >= 閾値？
  ├── 経済指標発表直前・直後は抑制
  └── 既存ポジションと矛盾しない？
        ↓
[trading/risk.py] リスク管理チェック
  ├── 日次最大損失に達していない？
  ├── ポジションサイズ計算（資金の X%）
  └── 最大同時ポジション数チェック
        ↓
[trading/order.py] 注文実行
  ├── OANDA v20 API で成行 or 指値注文
  ├── SL / TP を自動設定（AIの提案 or ユーザー設定値）
  └── 注文結果をログ
        ↓
[db/repository.py] Firestore に保存
  ├── signals コレクション（全AI判断を記録）
  ├── trades コレクション（実行済み取引）
  └── daily_pnl コレクション（日次損益）
        ↓
[monitoring/notifier.py] Slack 通知
  └── 注文実行・エラー・日次損益レポート
```

---

## 4. モジュール詳細

### 4.1 oanda_client.py

```python
# 主な責務
- get_candles(pair, granularity, count)  # ローソク足取得
- get_account_summary()                  # 口座情報（残高・証拠金）
- get_open_positions()                   # オープンポジション一覧
- create_order(pair, units, sl, tp)      # 注文送信
- close_position(trade_id)               # ポジションクローズ
```

### 4.2 ai/model_selector.py（ハイブリッド切替ロジック）

```python
# Sonnet を使う条件（それ以外は Haiku）
def select_model(context) -> str:
    # 1. 重要経済指標の前後2時間
    if has_high_impact_event_soon(context["economic_events"], hours=2):
        return "claude-sonnet-4-6"

    # 2. 高ボラティリティ（ATRが平均の1.5倍超）
    if context["technical"]["atr14"] > context["atr_avg"] * 1.5:
        return "claude-sonnet-4-6"

    # 3. Haikuで一次判断 → confidenceが境界値なら再判断
    #    (analyzer.py側でフォールバック処理)

    return "claude-haiku-4-5-20251001"
```

**月額コスト目安**

| ケース | Haiku | Sonnet | 合計/月 |
|--------|-------|--------|---------|
| 通常時 (80/20) | ~$3 | ~$9 | **~$12（≒1,800円）** |
| 全部Haiku | ~$4 | - | **~$4（≒600円）** |
| 全部Sonnet | - | ~$47 | **~$47（≒7,000円）** |

---

### 4.3 ai/analyzer.py

```python
# Claude に渡すコンテキスト構造
{
  "pair": "USD_JPY",
  "current_price": 149.50,
  "candles": {
    "1H":  [...],  # 直近48本
    "4H":  [...],  # 直近30本
    "1D":  [...],  # 直近20本
  },
  "technical": {
    "sma20": 149.20,
    "sma50": 148.80,
    "rsi14": 58.3,
    "atr14": 0.45
  },
  "economic_events": [...],   # 今後24時間の指標
  "recent_news": [...],       # 直近ニュース5件
  "open_positions": [...]     # 現在ポジション
}

# Claude の返却形式（JSON）
{
  "action": "BUY",           # BUY / SELL / HOLD
  "confidence": 0.75,        # 0.0 〜 1.0
  "timeframe": "DAY_TRADE",  # DAY_TRADE / SWING
  "suggested_sl_pips": 30,
  "suggested_tp_pips": 60,
  "reasoning": "..."         # 判断理由（日本語）
}
```

### 4.4 ai/fallback.py（Confidence フォールバック）

```python
# Haiku の confidence が境界値（0.60〜0.75）ならSonnetで再判断
async def analyze_with_fallback(context) -> Signal:
    model = select_model(context)
    result = await call_claude(model, context)

    if model == "claude-haiku-4-5-20251001" and 0.60 <= result.confidence <= 0.75:
        result = await call_claude("claude-sonnet-4-6", context)
        result.meta["fallback_used"] = True

    return result
```

---

### 4.5 trading/risk.py

```python
# リスク管理ロジック
- calc_position_size(balance, risk_pct, sl_pips, pair)
    # 例: 残高100万円 × 2% ÷ SL30pips → ロット数
- check_daily_loss_limit(daily_pnl, max_loss_jpy)
    # 日次損失が上限に達したら取引停止
- check_max_positions(open_positions, max_count)
    # 最大同時ポジション数チェック
```

### 4.6 db/repository.py (Firestore スキーマ)

```
signals/{signal_id}
  - pair, timestamp, action, confidence, reasoning
  - executed: bool
  - rejected_reason: str | null

trades/{trade_id}
  - oanda_trade_id, pair, direction, units
  - open_price, sl, tp
  - opened_at, closed_at
  - realized_pnl

daily_pnl/{YYYY-MM-DD}
  - date, realized_pnl, unrealized_pnl
  - trade_count, win_count, loss_count
```

---

## 5. クラウドインフラ (GCP)

```
Cloud Scheduler
  └── 15分毎に Cloud Run Job をキック

Cloud Run Job (fxautobuy-job)
  └── Docker コンテナで main.py 実行

Firestore
  └── 取引ログ・損益・シグナル記録

Secret Manager
  └── OANDA API Key, Claude API Key

Cloud Logging
  └── 全ログ集約

Cloud Monitoring + Alerting
  └── エラー発生時にメール通知
```

---

## 6. 設定パラメータ (config.py)

```python
# ユーザーが調整する値
PAIRS           = ["USD_JPY", "EUR_USD"]
RISK_PCT        = 2.0        # 1トレードあたり資金の2%リスク
MAX_DAILY_LOSS  = 10000      # 日次最大損失（円）
MAX_POSITIONS   = 3          # 最大同時ポジション数
CONFIDENCE_THRESHOLD = 0.70  # AIシグナルの採用閾値

# ハイブリッドモデル設定
PRIMARY_MODEL   = "claude-haiku-4-5-20251001"   # 通常時
FALLBACK_MODEL  = "claude-sonnet-4-6"           # 重要局面
FALLBACK_CONFIDENCE_MIN = 0.60  # これ以下のconfidenceでSonnetに切替
FALLBACK_CONFIDENCE_MAX = 0.75  # これ以上なら確信あり→Haikuのまま
TRADE_GRANULARITY = "H1"     # メイン足（H1=1時間足）
EXECUTION_INTERVAL = 15      # 実行間隔（分）

# SL/TP（AIの提案を使うか固定値を使うか）
USE_AI_SLTP     = True       # True: AI提案 / False: 固定値
DEFAULT_SL_PIPS = 30
DEFAULT_TP_PIPS = 60
```

---

## 7. ペーパートレードモード

本番稼働前に検証するためのドライランモード。

```python
PAPER_TRADE = True  # True: 注文を実際には送らない（ログのみ）
```

---

## 8. 開発フェーズ

| Phase | 内容 | 成果物 |
|-------|------|--------|
| 1 | OANDA API 接続・データ取得 | oanda_client.py, テスト |
| 2 | AI推論エンジン実装 | analyzer.py, prompts.py |
| 3 | リスク管理・注文実行 | risk.py, order.py |
| 4 | DB・ログ・通知 | repository.py, notifier.py |
| 5 | ペーパートレード検証 | バックテスト結果 |
| 6 | GCPデプロイ | Dockerfile, cloudbuild.yaml |
| 7 | 本番稼働・監視 | モニタリング設定 |
