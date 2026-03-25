import os
from dotenv import load_dotenv

load_dotenv()

# --- データソース ---
DATA_SOURCE = os.getenv("DATA_SOURCE", "yfinance")  # yfinance | oanda

# --- OANDA ---
OANDA_API_KEY     = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID  = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENVIRONMENT = os.getenv("OANDA_ENVIRONMENT", "practice")  # practice | live

# --- AI プロバイダー ---
AI_PROVIDER   = os.getenv("AI_PROVIDER", "groq")   # groq | claude
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- ハイブリッドモデル設定（Groq） ---
PRIMARY_MODEL  = "llama-3.1-8b-instant"       # 通常時（高速・軽量）
FALLBACK_MODEL = "llama-3.3-70b-versatile"    # 重要局面・境界値時

CONFIDENCE_THRESHOLD    = 0.70  # シグナル採用の最低閾値
FALLBACK_CONF_MIN       = 0.60  # これ以下は HOLD 扱い
FALLBACK_CONF_MAX       = 0.75  # この範囲ならSonnetで再判断

# --- 取引設定 ---
PAIRS               = ["USD_JPY", "EUR_USD"]
TRADE_GRANULARITY   = "H1"   # メイン足（H1=1時間足）
CANDLE_COUNTS       = {"H1": 48, "H4": 30, "D": 20}

# --- リスク管理 ---
RISK_PCT          = 2.0      # 1トレードあたり資金の2%リスク
MAX_DAILY_LOSS    = 10000    # 日次最大損失（円）
MAX_POSITIONS     = 3        # 最大同時ポジション数

# --- SL / TP ---
USE_AI_SLTP       = True     # True: AI提案 / False: 固定値
DEFAULT_SL_PIPS   = 30
DEFAULT_TP_PIPS   = 60

# --- 動作モード ---
PAPER_TRADE       = os.getenv("PAPER_TRADE", "true").lower() == "true"

# --- GCP ---
GCP_PROJECT_ID    = os.getenv("GCP_PROJECT_ID", "")

# --- Slack ---
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
