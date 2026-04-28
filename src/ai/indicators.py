"""ローソク足データからテクニカル指標を計算する"""
from __future__ import annotations

from dataclasses import dataclass

from src.data.oanda_client import Candle


@dataclass
class TechnicalIndicators:
    sma5:       float | None
    sma20:      float | None
    sma50:      float | None
    rsi14:      float | None
    atr14:      float | None
    macd_line:  float | None
    macd_signal: float | None
    macd_hist:  float | None       # 現在のヒストグラム
    macd_hist_prev: float | None   # 1本前のヒストグラム（クロス検出用）
    trend: str                     # "UP" | "DOWN" | "FLAT"


def calc_indicators(candles: list[Candle]) -> TechnicalIndicators:
    if not candles:
        return TechnicalIndicators(None, None, None, None, None, None, None, None, None, "FLAT")

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]

    sma5  = _sma(closes, 5)
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(highs, lows, closes, 14)
    trend = _trend(sma20, sma50, closes[-1] if closes else None)

    macd_line, macd_signal, macd_hist, macd_hist_prev = _macd(closes)

    return TechnicalIndicators(
        sma5 =round(sma5,  5) if sma5  else None,
        sma20=round(sma20, 5) if sma20 else None,
        sma50=round(sma50, 5) if sma50 else None,
        rsi14=round(rsi14, 2) if rsi14 else None,
        atr14=round(atr14, 5) if atr14 else None,
        macd_line  =round(macd_line,   6) if macd_line   is not None else None,
        macd_signal=round(macd_signal, 6) if macd_signal is not None else None,
        macd_hist  =round(macd_hist,   6) if macd_hist   is not None else None,
        macd_hist_prev=round(macd_hist_prev, 6) if macd_hist_prev is not None else None,
        trend=trend,
    )


# ------------------------------------------------------------------
# 内部計算
# ------------------------------------------------------------------

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
          ) -> tuple[float | None, float | None, float | None, float | None]:
    if len(closes) < slow + signal:
        return None, None, None, None

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)

    # EMAの長さを揃える
    diff = len(ema_fast) - len(ema_slow)
    macd_values = [f - s for f, s in zip(ema_fast[diff:], ema_slow)]

    if len(macd_values) < signal:
        return None, None, None, None

    signal_values = _ema(macd_values, signal)
    if len(signal_values) < 2:
        return None, None, None, None

    macd_line   = macd_values[-1]
    macd_signal = signal_values[-1]
    hist_curr   = macd_line - macd_signal

    # 1本前のヒストグラム
    macd_line_prev   = macd_values[-2]
    macd_signal_prev = signal_values[-2]
    hist_prev        = macd_line_prev - macd_signal_prev

    return macd_line, macd_signal, hist_curr, hist_prev


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    diffs  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in diffs]
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
