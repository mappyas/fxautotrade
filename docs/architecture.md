# FX AutoBuy システム設計書

## 1. システム概要

LLM（Claude API）を用いた FX 分析・売買支援システム。
Streamlit ダッシュボードで可視化し、Discord でアラート通知を行う。

- **データソース**: yfinance（開発・ペーパートレード）/ OANDA v20 API（本番）
- **AI エンジン**: Claude Sonnet 4.6（通常）/ Claude Haiku 4.5（フォールバック）
- **経済指標**: Finnhub API（当日〜翌日の high/medium インパクト指標）
- **アラート通知**: Discord Webhook
- **バックグラウンド実行**: GitHub Actions + cron-job.org（5分ごと）
- **ダッシュボード**: Streamlit Community Cloud
- **対象スタイル**: デイトレ（H1/H4/D）、スキャルピング（M15/M30/H1）
- **対象ペア**: USD/JPY、EUR/USD（拡張可能）

---

## 2. ディレクトリ構成

```
fxautobuy/
├── src/
│   ├── data/
│   │   ├── base_client.py          # データクライアント抽象基底クラス
│   │   ├── client_factory.py       # DATA_SOURCE に応じたクライアント生成
│   │   ├── oanda_client.py         # OANDA API ラッパー
│   │   ├── yfinance_client.py      # yfinance ラッパー（開発用）
│   │   └── economic_calendar.py    # Finnhub 経済指標取得
│   ├── ai/
│   │   ├── analyzer.py             # Claude API 呼び出し・レスポンス解析
│   │   ├── prompts.py              # プロンプトテンプレート
│   │   └── indicators.py           # テクニカル指標計算（SMA/RSI/ATR）
│   ├── trading/
│   │   ├── signal.py               # Signal データクラス
│   │   ├── order.py                # 注文実行
│   │   ├── risk.py                 # リスク管理
│   │   └── session.py              # 取引セッション判定
│   ├── notifications/
│   │   ├── discord.py              # Discord Webhook 送信
│   │   └── alert_filter.py         # テクニカルアラート条件判定・クールダウン管理
│   ├── config.py                   # 設定・環境変数
│   └── main.py                     # CLI エントリーポイント
├── scripts/
│   └── alert_job.py                # GitHub Actions から呼ばれるアラートジョブ
├── .github/
│   └── workflows/
│       └── alert.yml               # GitHub Actions ワークフロー（workflow_dispatch）
├── dashboard.py                    # Streamlit ダッシュボード
├── data/
│   ├── signal_log.json             # シグナル履歴ログ
│   └── alert_state.json            # アラートクールダウン状態
├── tests/
├── docs/
│   └── architecture.md
├── requirements.txt
└── pyproject.toml
```

---

## 3. システム構成図

```
【バックグラウンドアラート（AIなし）】

cron-job.org（5分ごと）
    → GitHub API（workflow_dispatch）
    → GitHub Actions（alert_job.py）
        → yfinance でローソク足取得
        → テクニカル指標計算（RSI/SMA/ATR）
        → 条件判定（RSI過買い・過売り、押し目・戻り売り）
        → 条件成立 + クールダウン解除 → Discord 通知
        → alert_state.json をキャッシュ（30分クールダウン管理）

【手動AI分析（ダッシュボード）】

Streamlit ダッシュボード
    → 「AI分析を実行」ボタン
    → Finnhub で経済指標取得
    → yfinance でローソク足取得（H1×100本 / H4×30本 / D×20本）
    → テクニカル指標計算
    → Claude API（Sonnet 4.6）で分析
        ├── confidence 境界値（0.55〜0.70）→ Haiku でフォールバック再判断
        └── シグナル（BUY/SELL/HOLD）+ SL/TP + 判断理由
    → ペーパートレード実行（or OANDA 注文）
    → signal_log.json に記録
```

---

## 4. AI 分析エンジン

### 4.1 モデル構成

| ロール | モデル | 使用タイミング |
|--------|--------|----------------|
| Primary | claude-sonnet-4-6 | 通常分析 |
| Fallback | claude-haiku-4-5-20251001 | confidence が境界値（0.55〜0.70）のとき |

### 4.2 Claude に渡すコンテキスト（デイトレモード）

```json
{
  "pair": "USD_JPY",
  "trade_mode": "daytrading",
  "current_price": 155.32,
  "candles": {
    "H1": [...],   // 直近20本
    "H4": [...],   // 直近12本
    "D":  [...]    // 直近8本
  },
  "technical": {
    "sma20": 155.10,
    "sma50": 154.80,
    "rsi14": 58.3,
    "atr14": 0.45,
    "trend": "UP"
  },
  "economic_events": [...],
  "open_positions": [...]
}
```

### 4.3 Claude の返却形式

```json
{
  "action": "BUY",
  "confidence": 0.78,
  "timeframe": "DAY_TRADE",
  "suggested_sl_pips": 50,
  "suggested_tp_pips": 100,
  "reasoning": "判断理由（日本語100字程度）"
}
```

---

## 5. トレードモード

| | デイトレ | スキャルピング |
|---|---|---|
| 使用足 | H1 / H4 / D | M15 / M30 / H1 |
| デフォルトSL | 50 pips | 12 pips |
| デフォルトTP | 100 pips | 24 pips |
| AI指示SL目安 | 40〜70 pips | 8〜15 pips |
| AI指示TP目安 | 80〜140 pips | 16〜30 pips |

---

## 6. テクニカルアラート条件

AIを呼ばずにテクニカル指標のみで判定。条件成立時に Discord 通知。

### 6.1 エントリー条件（優先順位順）

| 優先 | 条件キー | 判定 | エントリー方向 |
|------|----------|------|---------------|
| 1 | MACD_BULL | MACDヒストグラムがマイナス→プラスにクロス + SMA5 > SMA20 | BUY |
| 1 | MACD_BEAR | MACDヒストグラムがプラス→マイナスにクロス + SMA5 < SMA20 | SELL |
| 2 | RSI_OVERSOLD | RSI < 35 + SMA5 > SMA20 | BUY |
| 2 | RSI_OVERBOUGHT | RSI > 65 + SMA5 < SMA20 | SELL |

### 6.2 トレンドフィルター

上位足トレンド（SMA20/SMA50 + 現在値で判定）と逆方向のエントリーは禁止。

| trend値 | 禁止シグナル | 理由 |
|---------|-------------|------|
| DOWN | BUY系（MACD_BULL / RSI_OVERSOLD） | 下降トレンド中の一時的な反発でのエントリーを防ぐ |
| UP | SELL系（MACD_BEAR / RSI_OVERBOUGHT） | 上昇トレンド中の一時的な下押しでのエントリーを防ぐ |
| FLAT | 制限なし | レンジ相場はどちらの条件も許可 |

- **クールダウン**: 同一ペアで条件発火後 30分間は再通知しない
- **状態管理**: `data/alert_state.json` に保存（GitHub Actions キャッシュで永続化）

---

## 7. 設定パラメータ（config.py）

```python
# AI
AI_PROVIDER          = "claude"       # claude / groq / gemini
PRIMARY_MODEL        = "claude-sonnet-4-6"
FALLBACK_MODEL       = "claude-haiku-4-5-20251001"
CONFIDENCE_THRESHOLD = 0.65           # シグナル採用の最低閾値
FALLBACK_CONF_MIN    = 0.55           # これ以下は HOLD 扱い
FALLBACK_CONF_MAX    = 0.70           # この範囲なら Haiku で再判断

# データ
DATA_SOURCE          = "yfinance"     # yfinance / oanda
PAIRS                = ["USD_JPY", "EUR_USD"]
CANDLE_COUNTS        = {"H1": 100, "H4": 30, "D": 20}
SCALP_CANDLE_COUNTS  = {"M15": 60, "M30": 24, "H1": 12}

# SL/TP
DEFAULT_SL_PIPS      = 50
DEFAULT_TP_PIPS      = 100
SCALP_SL_PIPS        = 12
SCALP_TP_PIPS        = 24

# リスク管理
RISK_PCT             = 2.0            # 1トレードあたり資金の2%
MAX_DAILY_LOSS       = 10000          # 日次最大損失（円）
MAX_POSITIONS        = 3              # 最大同時ポジション数

# 動作モード
PAPER_TRADE          = True           # True: ペーパートレード
```

---

## 8. 経済指標（Finnhub API）

- **取得範囲**: 当日〜翌日
- **フィルタ**: high / medium インパクト、主要国（US/JP/EU/GB/AU/CA/CH/NZ）
- **用途**:
  - AI プロンプトに含めて判断材料にする
  - 高インパクト指標がある場合は上位モデル（Sonnet）を優先使用
  - ダッシュボードのカレンダーパネルで一覧表示

---

## 9. バックグラウンドアラート実行フロー

```
cron-job.org（5分ごと）
    POST https://api.github.com/repos/mappyas/fxautotrade/actions/workflows/alert.yml/dispatches
    Authorization: Bearer {GITHUB_TOKEN}
    Body: {"ref": "master"}
        ↓
GitHub Actions（ubuntu-latest）
    pip install -r requirements.txt
    python scripts/alert_job.py
        ↓
    各ペアの H1×100本 取得（yfinance）
    RSI / SMA / ATR 計算
    条件判定 → Discord 通知（条件成立時のみ）
    alert_state.json をキャッシュに保存
```

---

## 10. ペーパートレードモード

```python
PAPER_TRADE = True  # 注文を実際には送らずログのみ記録
```

シグナル履歴は `data/signal_log.json` に最大500件保存。
ダッシュボードの「シグナル履歴」テーブルで確認可能。

---

## 11. PDCAサイクル設計（次期フェーズ）

### 11.1 基本方針

AIを「その場の判断者」ではなく「戦略立案者・評価者」として活用する設計。
執行（Do）はルールベースで行い、コスト・速度・再現性を確保する。

```
Plan（AI）→ Do（ルールベース）→ Check（AI）→ Action（次のPlanへ反映）
```

### 11.2 取引時間帯

**東京セッション開始〜深夜0時（JST 9:00〜24:00）**

| セッション | 時間帯（JST） | 特徴 |
|-----------|--------------|------|
| 東京 | 9:00〜15:00 | USD/JPY中心、比較的穏やか |
| ロンドン | 16:00〜翌1:00 | 方向感が決まりやすい |
| ロンドン/NY重複 | 21:00〜翌1:00 | ボラティリティ最大 |

### 11.3 Planフェーズ実行タイミング（1日4回）

| 時刻（JST） | 役割 | 重要度 |
|------------|------|--------|
| 08:30 | 東京セッション前。前日NY終値確認、当日指標確認、大方針決定 | ★★★ |
| 15:30 | ロンドン開始前。東京時間結果を受けて方針更新。当日の方向感が決まりやすい | ★★★★ |
| 20:30 | NY開始前。米経済指標の確認、SL/TP幅の調整判断 | ★★★★ |
| 23:00 | 最終確認。翌日持ち越し or 手仕舞いの判断。Checkも兼ねる | ★★★ |

### 11.4 各フェーズの詳細

#### Plan（AI）
- **入力**: 直近ローソク足、経済指標スケジュール、前回Checkの結果
- **出力**: 当日の方向バイアス（BUY寄り / SELL寄り / 様子見）、エントリー禁止条件、目安価格レベル
- **ポイント**: 「何をするか」より「何をしてはいけないか」を明確にする

#### Do（ルールベース）
- **入力**: Planの出力（制約条件）
- **動作**: テクニカル条件がPlanの制約内で成立した場合のみエントリー
- **AIは使わない**: 速度・コスト・再現性のため

#### Check（AI）
- **入力**: Planの出力、実際のエントリー・決済記録、実際の値動き
- **出力**: Planとの乖離分析、勝敗の仮説、次回Planへの改善提案
- **実行タイミング**: 23:00の最終Planと兼ねる、または決済のたびに実行

#### Action（自動フィードバック）
- CheckのAI出力を次回Planのプロンプトコンテキストに自動で追加
- 直近N件のCheckログを蓄積し、傾向を学習させる

### 11.5 データファイル構成（次期追加予定）

```
data/
  sim_results.json     # 取引記録（既存）
  alert_state.json     # アラートクールダウン状態（既存）
  plan_history.json    # 各Planフェーズの出力を蓄積（新規）
  check_log.json       # Checkフェーズの分析結果を蓄積（新規）
```
