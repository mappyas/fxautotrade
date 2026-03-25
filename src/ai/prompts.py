"""プロンプトテンプレート"""
from __future__ import annotations

import json

from src.ai.indicators import TechnicalIndicators
from src.data.oanda_client import Candle, Position


SYSTEM_PROMPT = """\
あなたはプロのFXトレーダーです。提供されたデータを分析し、トレードシグナルをJSONで出力してください。

## 出力フォーマット（必ずこの形式で返すこと）
```json
{
  "action": "BUY" または "SELL" または "HOLD",
  "confidence": 0.0〜1.0の数値,
  "timeframe": "DAY_TRADE" または "SWING",
  "suggested_sl_pips": 整数（ストップロスpips）,
  "suggested_tp_pips": 整数（テイクプロフィットpips）,
  "reasoning": "判断理由を日本語で100字程度"
}
```

## 判断基準
- confidence 0.75以上：強いシグナル → BUY/SELL
- confidence 0.60〜0.75：弱いシグナル → 基本HOLD
- confidence 0.60未満：不確実 → HOLD
- 重要経済指標の直前直後はリスクを考慮すること
- トレンドと逆張りする場合は confidence を下げること
"""


def build_user_prompt(
    pair: str,
    candles_h1: list[Candle],
    candles_h4: list[Candle],
    candles_d: list[Candle],
    indicators_h1: TechnicalIndicators,
    open_positions: list[Position],
    economic_events: list[dict] | None = None,
    news: list[str] | None = None,
) -> str:
    current_price = candles_h1[-1].close if candles_h1 else 0.0

    # ローソク足は直近10本に絞ってトークン節約
    def fmt_candles(candles: list[Candle], n: int = 10) -> list[dict]:
        return [
            {
                "time": c.time.strftime("%Y-%m-%d %H:%M"),
                "o": c.open, "h": c.high, "l": c.low, "c": c.close,
            }
            for c in candles[-n:]
        ]

    context = {
        "pair": pair,
        "current_price": current_price,
        "candles": {
            "H1": fmt_candles(candles_h1, 10),
            "H4": fmt_candles(candles_h4, 8),
            "D":  fmt_candles(candles_d,  5),
        },
        "technical": {
            "sma20":  indicators_h1.sma20,
            "sma50":  indicators_h1.sma50,
            "rsi14":  indicators_h1.rsi14,
            "atr14":  indicators_h1.atr14,
            "trend":  indicators_h1.trend,
        },
        "open_positions": [
            {
                "instrument": p.instrument,
                "direction":  p.direction,
                "units":      p.units,
                "open_price": p.open_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in open_positions
        ],
        "economic_events": economic_events or [],
        "news": news or [],
    }

    return f"以下のデータを分析してトレードシグナルを出力してください。\n\n```json\n{json.dumps(context, ensure_ascii=False, indent=2)}\n```"
