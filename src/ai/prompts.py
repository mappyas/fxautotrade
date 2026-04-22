"""プロンプトテンプレート"""
from __future__ import annotations

import json

from src.ai.indicators import TechnicalIndicators
from src.data.oanda_client import Candle, Position


SYSTEM_PROMPT = """\
あなたはプロのFXトレーダーです。提供されたデータを分析し、トレードシグナルをJSONで出力してください。

## 厳守事項
- 必ず以下のJSONのみを返すこと。説明文・マークダウン・前置き・後書きは一切不要。
- コードブロック（```json）で囲むこと。

## 出力フォーマット
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
- confidence 0.65以上：シグナル採用 → BUY/SELL
- confidence 0.55〜0.65：弱いシグナル → 基本HOLD
- confidence 0.55未満：不確実 → HOLD
- 重要経済指標の直前直後はリスクを考慮すること
- トレンドと逆張りする場合は confidence を下げること

## SL/TP の目安（当日〜翌日決済のデイトレ想定）
- suggested_sl_pips: 40〜70pips（通常50pips基準、ボラ高時は70pips）
- suggested_tp_pips: 80〜140pips（リスクリワード比 1:2 を維持）
- ATRが大きい（高ボラ）場合は広め、レンジ相場は狭めに調整すること
"""


_TIMEFRAME_LABELS = {
    "daytrading": ("H1", "H4", "D"),
    "scalping":   ("M15", "M30", "H1"),
}

_SLTP_GUIDE = {
    "daytrading": "- suggested_sl_pips: 40〜70pips（通常50pips基準、ボラ高時は70pips）\n- suggested_tp_pips: 80〜140pips（リスクリワード比 1:2 を維持）\n- ATRが大きい（高ボラ）場合は広め、レンジ相場は狭めに調整すること",
    "scalping":   "- suggested_sl_pips: 8〜15pips（通常12pips基準）\n- suggested_tp_pips: 16〜30pips（リスクリワード比 1:2 を維持）\n- スプレッドを考慮し、ブレイクアウト直後や強いモメンタムがある場合のみエントリー推奨",
}


def build_user_prompt(
    pair: str,
    candles_h1: list[Candle],
    candles_h4: list[Candle],
    candles_d: list[Candle],
    indicators_h1: TechnicalIndicators,
    open_positions: list[Position],
    economic_events: list[dict] | None = None,
    news: list[str] | None = None,
    trade_mode: str = "daytrading",
) -> str:
    current_price = candles_h1[-1].close if candles_h1 else 0.0
    tf_short, tf_mid, tf_long = _TIMEFRAME_LABELS.get(trade_mode, _TIMEFRAME_LABELS["daytrading"])

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
        "trade_mode": trade_mode,
        "current_price": current_price,
        "candles": {
            tf_short: fmt_candles(candles_h1, 20),
            tf_mid:   fmt_candles(candles_h4, 12),
            tf_long:  fmt_candles(candles_d,   8),
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

    sltp_guide = _SLTP_GUIDE.get(trade_mode, _SLTP_GUIDE["daytrading"])
    mode_note = f"\n\n## 今回のモード: {trade_mode}\n{sltp_guide}"

    return f"以下のデータを分析してトレードシグナルを出力してください。{mode_note}\n\n```json\n{json.dumps(context, ensure_ascii=False, indent=2)}\n```"
