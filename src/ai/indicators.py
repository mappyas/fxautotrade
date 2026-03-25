"""ローソク足データからテクニカル指標を計算する"""
from __future__ import annotations

from dataclasses import dataclass

from src.data.oanda_client import Candle


@dataclass
class TechnicalIndicators:
    sma20: float | None
    sma50: float | None
    rsi14: float | None
    atr14: float | None
    trend: str          # "UP" | "DOWN" | "FLAT"


def calc_indicators(candles: list[Candle]) -> TechnicalIndicators:
    if not candles:
        return TechnicalIndicators(None, None, None, None, "FLAT")

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]

    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(highs, lows, closes, 14)
    trend = _trend(sma20, sma50, closes[-1] if closes else None)

    return TechnicalIndicators(
        sma20=round(sma20, 5) if sma20 else None,
        sma50=round(sma50, 5) if sma50 else None,
        rsi14=round(rsi14, 2) if rsi14 else None,
        atr14=round(atr14, 5) if atr14 else None,
        trend=trend,
    )


# ------------------------------------------------------------------
# 内部計算
# ------------------------------------------------------------------

def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in diffs]
    losses = [-d if d < 0 else 0.0 for d in diffs]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    return sum(trs[-period:]) / period


def _trend(sma20: float | None, sma50: float | None, current: float | None) -> str:
    if sma20 is None or sma50 is None or current is None:
        return "FLAT"
    if sma20 > sma50 and current > sma20:
        return "UP"
    if sma20 < sma50 and current < sma20:
        return "DOWN"
    return "FLAT"
