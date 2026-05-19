"""Planフェーズ: セッション開始時の取引方針を立案する"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
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
あなたはプロのFXトレーダーです。指定されたセッションの取引方針をJSONで出力してください。

## 厳守事項
- 必ず以下のJSONのみを返すこと。説明文・マークダウン・前置き・後書きは一切不要。
- コードブロック（```json）で囲むこと。

## 出力フォーマット
```json
{
  "session": "セッション名（文字列）",
  "bias": "BUY" または "SELL" または "NEUTRAL",
  "avoid_until": null または "2026-05-19T21:45:00+09:00"（経済指標禁止期間の終了時刻・ISO形式）,
  "notes": "方針の説明（日本語100字程度）"
}
```

## 判断基準
- bias: D足・H4足のトレンドを最重視し、現セッションでどちら方向が優位かを判断する
- NEUTRAL: 上位足とH1が矛盾している、またはレンジ相場のとき
- avoid_until: 高インパクト経済指標がある場合、発表30分前〜発表後15分の終了時刻を設定する
- 上位足トレンドと逆方向は bias=NEUTRAL で表現する（強制禁止ではなく消極的判断）
"""


@dataclass
class PlanResult:
    session:     str
    bias:        str        # "BUY" | "SELL" | "NEUTRAL"
    avoid_until: str | None # ISO timestamp or None
    notes:       str
    pair:        str
    timestamp:   str


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
        "economic_events":   economic_events,
        "recent_check_log":  _load_recent_check_log(5),
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
        max_tokens=512,
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
        return PlanResult(session=session, bias="NEUTRAL", avoid_until=None,
                          notes="解析失敗", pair=pair, timestamp=now_str)

    bias = data.get("bias", "NEUTRAL")
    if bias not in ("BUY", "SELL", "NEUTRAL"):
        bias = "NEUTRAL"

    return PlanResult(
        session=data.get("session", session),
        bias=bias,
        avoid_until=data.get("avoid_until"),
        notes=data.get("notes", ""),
        pair=pair,
        timestamp=now_str,
    )


# ------------------------------------------------------------------
# メイン
# ------------------------------------------------------------------

def run_plan(
    pair: str,
    session: str,
    candles_h1: list[Candle],
    candles_h4: list[Candle],
    candles_d:  list[Candle],
    ind: TechnicalIndicators,
    economic_events: list[dict],
) -> PlanResult:
    user_prompt = build_plan_prompt(
        pair, session, candles_h1, candles_h4, candles_d, ind, economic_events
    )

    try:
        raw = _call_claude_plan(user_prompt)
        logger.debug("Plan raw response: %s", raw[:300])
        plan = _parse_plan(raw, pair, session)
    except Exception as e:
        logger.error("Plan API呼び出し失敗 %s: %s", pair, e)
        plan = PlanResult(
            session=session, bias="NEUTRAL", avoid_until=None,
            notes=f"API呼び出し失敗: {e}", pair=pair,
            timestamp=datetime.now(JST).isoformat(),
        )

    _save_plan_state(pair, plan)
    _append_plan_history(plan)
    logger.info("Plan保存完了: %s bias=%s avoid_until=%s", pair, plan.bias, plan.avoid_until)
    return plan
