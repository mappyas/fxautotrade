"""Planフェーズ: セッション開始時の取引方針を立案する"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.ai.indicators import TechnicalIndicators
from src.data.oanda_client import Candle

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

PLAN_STATE_FILE   = Path("data/plan_state.json")
PLAN_HISTORY_FILE = Path("data/plan_history.json")
CHECK_LOG_FILE    = Path("data/check_log.json")

SESSIONS: dict[str, str] = {
    "TOKYO":       "東京セッション開始前（9:00〜）",
    "LONDON_OPEN": "ロンドンセッション開始前（16:00〜）",
    "NY_OPEN":     "NYセッション開始前（21:00〜）",
    "FINAL":       "本日の最終確認（23:00〜翌0:00）",
}

_SYSTEM_PROMPT = """\
あなたはプロのFXスキャルパーです。指定されたセッションの取引方針をJSONで出力してください。

## 厳守事項
- 必ず以下のJSONのみを返すこと。説明文・マークダウン・前置き・後書きは一切不要。
- コードブロック（```json）で囲むこと。

## 出力フォーマット
```json
{
  "session": "セッション名（文字列）",
  "fundamental": "ファンダメンタル視点の分析（経済指標・中央銀行動向・市場センチメント・介入リスク等）",
  "technical": "テクニカル視点の分析（H1でトレンド確認、M5でエントリー状況を説明）",
  "bias": "BUY" または "SELL" または "NEUTRAL",
  "avoid_until": null または "2026-05-21T21:45:00+09:00"（高インパクト指標の禁止期間終了時刻・ISO形式）,
  "plans": [
    {
      "label": "A",
      "condition": "最優先エントリー条件（具体的なテクニカル条件）",
      "entry": "BUY" または "SELL" または "HOLD",
      "sl_pips": 10,
      "tp_pips": 20,
      "notes": "補足・注意事項"
    },
    {
      "label": "B",
      "condition": "サブシナリオの条件",
      "entry": "BUY" または "SELL" または "HOLD",
      "sl_pips": 12,
      "tp_pips": 24,
      "notes": "補足・注意事項"
    },
    {
      "label": "C",
      "condition": "上記が揃わない場合",
      "entry": "HOLD",
      "sl_pips": null,
      "tp_pips": null,
      "notes": "見送り条件"
    }
  ]
}
```

## 判断基準
- **トレンド確認**: H1足でトレンド方向を決定し、M5足でエントリータイミングを探す
- **bias**: H1のトレンドが優位な方向。上位足と短期足が矛盾している・レンジ相場の場合はNEUTRAL
- **SL/TP目安（スキャル）**: SL 8〜15pips / TP 16〜30pips（RR 1:2以上を維持）
- **avoid_until**: 高インパクト経済指標がある場合、発表30分前〜発表後15分の終了時刻を設定する
- **Plan A**: 最も確度が高いシナリオ。条件が揃い次第エントリー
- **Plan B**: AのサブシナリオまたはAが不発の場合の代替案
- **Plan C**: 見送り条件（上記が揃わない場合は無理にエントリーしない）
- 上位足トレンドと逆方向のエントリーは原則禁止
"""


@dataclass
class PlanResult:
    session:     str
    fundamental: str
    technical:   str
    bias:        str              # "BUY" | "SELL" | "NEUTRAL"
    avoid_until: str | None       # ISO timestamp or None
    plans:       list[dict] = field(default_factory=list)
    pair:        str = ""
    timestamp:   str = ""


# ------------------------------------------------------------------
# 状態管理
# ------------------------------------------------------------------

def load_plan_state() -> dict:
    if PLAN_STATE_FILE.exists():
        try:
            return json.loads(PLAN_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_plan_state(pair: str, plan: PlanResult) -> None:
    state = load_plan_state()
    state[pair] = asdict(plan)
    PLAN_STATE_FILE.parent.mkdir(exist_ok=True)
    PLAN_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _append_plan_history(plan: PlanResult) -> None:
    history: list = []
    if PLAN_HISTORY_FILE.exists():
        try:
            history = json.loads(PLAN_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    history.append(asdict(plan))
    PLAN_HISTORY_FILE.parent.mkdir(exist_ok=True)
    PLAN_HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_recent_check_log(n: int = 5) -> list[dict]:
    if not CHECK_LOG_FILE.exists():
        return []
    try:
        logs = json.loads(CHECK_LOG_FILE.read_text(encoding="utf-8"))
        return logs[-n:]
    except Exception:
        return []


# ------------------------------------------------------------------
# プロンプト構築
# ------------------------------------------------------------------

def build_plan_prompt(
    pair: str,
    session: str,
    candles_m5: list[Candle],
    candles_h1: list[Candle],
    candles_h4: list[Candle],
    candles_d:  list[Candle],
    ind: TechnicalIndicators,
    economic_events: list[dict],
) -> str:
    session_label = SESSIONS.get(session, session)
    pair_label = pair.replace("_", "/")

    def fmt(candles: list[Candle], n: int) -> list[dict]:
        return [
            {"time": c.time.strftime("%Y-%m-%d %H:%M"),
             "o": c.open, "h": c.high, "l": c.low, "c": c.close}
            for c in candles[-n:]
        ]

    context = {
        "pair":         pair_label,
        "session":      session_label,
        "current_time": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "candles": {
            "M5": fmt(candles_m5, 60),
            "H1": fmt(candles_h1, 20),
            "H4": fmt(candles_h4, 12),
            "D":  fmt(candles_d,   8),
        },
        "technical": {
            "sma5":      ind.sma5,
            "sma20":     ind.sma20,
            "sma50":     ind.sma50,
            "rsi14":     ind.rsi14,
            "atr14":     ind.atr14,
            "macd_hist": ind.macd_hist,
            "trend":     ind.trend,
        },
        "economic_events":  economic_events,
        "recent_check_log": _load_recent_check_log(5),
    }

    return (
        f"セッション「{session_label}」の {pair_label} 取引方針を出力してください。\n\n"
        f"```json\n{json.dumps(context, ensure_ascii=False, indent=2)}\n```"
    )


# ------------------------------------------------------------------
# API呼び出し & パース
# ------------------------------------------------------------------

def _call_claude_plan(user_prompt: str) -> str:
    import anthropic
    from src.config import ANTHROPIC_API_KEY, PRIMARY_MODEL
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=PRIMARY_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text


def _parse_plan(raw: str, pair: str, session: str) -> PlanResult:
    now_str = datetime.now(JST).isoformat()

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    json_str = match.group(1) if match else raw.strip()
    if not match:
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            json_str = brace.group(0)

    try:
        data = json.loads(json_str)
    except Exception:
        logger.warning("Planレスポンス解析失敗: %s", raw[:200])
        return PlanResult(
            session=session, fundamental="解析失敗", technical="解析失敗",
            bias="NEUTRAL", avoid_until=None, plans=[], pair=pair, timestamp=now_str,
        )

    bias = data.get("bias", "NEUTRAL")
    if bias not in ("BUY", "SELL", "NEUTRAL"):
        bias = "NEUTRAL"

    return PlanResult(
        session=data.get("session", session),
        fundamental=data.get("fundamental", ""),
        technical=data.get("technical", ""),
        bias=bias,
        avoid_until=data.get("avoid_until"),
        plans=data.get("plans", []),
        pair=pair,
        timestamp=now_str,
    )


# ------------------------------------------------------------------
# メイン
# ------------------------------------------------------------------

def run_plan(
    pair: str,
    session: str,
    candles_m5: list[Candle],
    candles_h1: list[Candle],
    candles_h4: list[Candle],
    candles_d:  list[Candle],
    ind: TechnicalIndicators,
    economic_events: list[dict],
) -> PlanResult:
    user_prompt = build_plan_prompt(
        pair, session, candles_m5, candles_h1, candles_h4, candles_d, ind, economic_events
    )

    try:
        raw = _call_claude_plan(user_prompt)
        logger.debug("Plan raw response: %s", raw[:300])
        plan = _parse_plan(raw, pair, session)
    except Exception as e:
        logger.error("Plan API呼び出し失敗 %s: %s", pair, e)
        plan = PlanResult(
            session=session, fundamental="", technical="",
            bias="NEUTRAL", avoid_until=None, plans=[],
            pair=pair, timestamp=datetime.now(JST).isoformat(),
        )

    _save_plan_state(pair, plan)
    _append_plan_history(plan)
    logger.info("Plan保存完了: %s bias=%s avoid_until=%s", pair, plan.bias, plan.avoid_until)
    return plan
