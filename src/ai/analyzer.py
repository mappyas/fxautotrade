"""AI推論エンジン（Groq / Claude 対応）"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from src.ai.indicators import TechnicalIndicators, calc_indicators
from src.ai.prompts import SYSTEM_PROMPT, build_user_prompt
from src.config import (
    AI_PROVIDER,
    CONFIDENCE_THRESHOLD,
    FALLBACK_CONF_MAX,
    FALLBACK_CONF_MIN,
    FALLBACK_MODEL,
    GROQ_API_KEY,
    PRIMARY_MODEL,
)
from src.data.oanda_client import Candle, Position


@dataclass
class Signal:
    action: str               # "BUY" | "SELL" | "HOLD"
    confidence: float         # 0.0 〜 1.0
    timeframe: str            # "DAY_TRADE" | "SWING"
    suggested_sl_pips: int
    suggested_tp_pips: int
    reasoning: str
    model_used: str = ""
    fallback_used: bool = False

    @property
    def is_actionable(self) -> bool:
        return self.action != "HOLD" and self.confidence >= CONFIDENCE_THRESHOLD


def analyze(
    pair: str,
    candles_h1: list[Candle],
    candles_h4: list[Candle],
    candles_d: list[Candle],
    open_positions: list[Position],
    economic_events: list[dict] | None = None,
    news: list[str] | None = None,
) -> Signal:
    indicators = calc_indicators(candles_h1)
    model = _select_model(indicators, economic_events or [])
    signal = _call_llm(model, pair, candles_h1, candles_h4, candles_d, indicators, open_positions, economic_events, news)

    # Confidence が境界値ならフォールバックモデルで再判断
    if model == PRIMARY_MODEL and FALLBACK_CONF_MIN <= signal.confidence <= FALLBACK_CONF_MAX:
        signal = _call_llm(FALLBACK_MODEL, pair, candles_h1, candles_h4, candles_d, indicators, open_positions, economic_events, news)
        signal.fallback_used = True

    return signal


# ------------------------------------------------------------------
# モデル選択
# ------------------------------------------------------------------

def _select_model(indicators: TechnicalIndicators, economic_events: list[dict]) -> str:
    # 重要経済指標がある → 上位モデル
    high_impact = [e for e in economic_events if e.get("impact") == "HIGH"]
    if high_impact:
        return FALLBACK_MODEL

    # 高ボラティリティ（ATRが大きい）→ 上位モデル
    # ※ ATRの絶対値でなく相対比較は将来実装
    return PRIMARY_MODEL


# ------------------------------------------------------------------
# LLM 呼び出し
# ------------------------------------------------------------------

def _call_llm(
    model: str,
    pair: str,
    candles_h1: list[Candle],
    candles_h4: list[Candle],
    candles_d: list[Candle],
    indicators: TechnicalIndicators,
    open_positions: list[Position],
    economic_events: list[dict] | None,
    news: list[str] | None,
) -> Signal:
    user_prompt = build_user_prompt(
        pair, candles_h1, candles_h4, candles_d,
        indicators, open_positions, economic_events, news,
    )

    if AI_PROVIDER == "groq":
        content = _call_groq(model, user_prompt)
    elif AI_PROVIDER == "claude":
        content = _call_claude(model, user_prompt)
    else:
        raise ValueError(f"未対応の AI_PROVIDER: {AI_PROVIDER}")

    signal = _parse_response(content)
    signal.model_used = model
    return signal


def _call_groq(model: str, user_prompt: str) -> str:
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=512,
    )
    return resp.choices[0].message.content


def _call_claude(model: str, user_prompt: str) -> str:
    import anthropic
    from src.config import ANTHROPIC_API_KEY
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------
# レスポンス解析
# ------------------------------------------------------------------

def _parse_response(content: str) -> Signal:
    # コードブロック内のJSONを抽出
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    json_str = match.group(1) if match else content

    # フォールバック: 生テキストからJSONを抽出
    if not match:
        brace_match = re.search(r"\{.*\}", content, re.DOTALL)
        if brace_match:
            json_str = brace_match.group(0)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return Signal(
            action="HOLD", confidence=0.0, timeframe="DAY_TRADE",
            suggested_sl_pips=30, suggested_tp_pips=60,
            reasoning=f"レスポンス解析失敗: {content[:100]}",
        )

    return Signal(
        action=data.get("action", "HOLD").upper(),
        confidence=float(data.get("confidence", 0.0)),
        timeframe=data.get("timeframe", "DAY_TRADE"),
        suggested_sl_pips=int(data.get("suggested_sl_pips", 30)),
        suggested_tp_pips=int(data.get("suggested_tp_pips", 60)),
        reasoning=data.get("reasoning", ""),
    )
