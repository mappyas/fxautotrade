"""
ルールベースシグナルエンジン - Case 1: BB レンジ逆張り

ADX(14) < 20  →  レンジ相場
BB(20,2) ±2σ タッチ  +  RSI(14) 30/70  →  逆張りシグナル

エントリー条件:
  BUY : setup.low  <= BB_lower  AND RSI <= 30  AND ADX < 20
        + 次の足が陽線（反転確認）
  SELL: setup.high >= BB_upper  AND RSI >= 70  AND ADX < 20
        + 次の足が陰線（反転確認）

TP: BB 中央線（MA20）
SL: バンド外側 + バッファ
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.data.oanda_client import Candle


# ------------------------------------------------------------------
# シグナルデータクラス
# ------------------------------------------------------------------

@dataclass
class RangeSignal:
    direction:    str    # "BUY" | "SELL"
    entry_price:  float
    sl_price:     float
    tp_price:     float
    rr:           float  # reward/risk ratio
    setup_time:   str    # setup バーの ISO timestamp
    confirm_time: str    # confirmation バーの ISO timestamp


# ------------------------------------------------------------------
# 指標計算（ライブ用・シンプル）
# ------------------------------------------------------------------

def calc_bb(
    closes: list[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[float, float, float] | None:
    """Bollinger Bands → (upper, middle, lower) or None"""
    if len(closes) < period:
        return None
    w = closes[-period:]
    mid = sum(w) / period
    var = sum((v - mid) ** 2 for v in w) / period
    s = math.sqrt(var)
    return mid + std_dev * s, mid, mid - std_dev * s


def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    """RSI(period) - Wilder's smoothing"""
    if len(closes) < period + 1:
        return None

    diffs  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in diffs]
    losses = [max(-d, 0.0) for d in diffs]

    avg_g = sum(gains[:period])  / period
    avg_l = sum(losses[:period]) / period

    for i in range(period, len(diffs)):
        avg_g = (avg_g * (period - 1) + gains[i])  / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    return _rsi_from_avgs(avg_g, avg_l)


def calc_adx(candles: list[Candle], period: int = 14) -> float | None:
    """ADX(period) - Wilder's smoothing"""
    if len(candles) < period * 2 + 1:
        return None

    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]
    closes = [c.close for c in candles]
    n = len(candles)

    trs, pdms, mdms = [], [], []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        trs.append(tr)
        pdms.append(up   if (up   > down and up   > 0) else 0.0)
        mdms.append(down if (down > up   and down > 0) else 0.0)

    if len(trs) < period:
        return None

    # 第1段階: TR / +DM / -DM の Wilder's smooth
    s_tr  = sum(trs[:period])  / period
    s_pdm = sum(pdms[:period]) / period
    s_mdm = sum(mdms[:period]) / period

    dx_vals: list[float] = [_dx(s_tr, s_pdm, s_mdm)]

    for j in range(period, len(trs)):
        s_tr  = (s_tr  * (period - 1) + trs[j])  / period
        s_pdm = (s_pdm * (period - 1) + pdms[j]) / period
        s_mdm = (s_mdm * (period - 1) + mdms[j]) / period
        dx_vals.append(_dx(s_tr, s_pdm, s_mdm))

    if len(dx_vals) < period:
        return None

    # 第2段階: DX の Wilder's smooth = ADX
    adx_val = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period

    return adx_val


# ------------------------------------------------------------------
# シグナル検出（ライブ取引用）
# ------------------------------------------------------------------

def detect_range_reversal(
    candles: list[Candle],
    pair: str,
    bb_period: int = 20,
    adx_period: int = 14,
    rsi_period: int = 14,
    adx_threshold: float = 20.0,
    rsi_ob: float = 70.0,
    rsi_os: float = 30.0,
    sl_buffer_pips: float = 2.0,
    rr_min: float = 2.0,
) -> RangeSignal | None:
    """
    直近3本でシグナル検出（ライブ取引用）

    candles[-3]: setup バー
    candles[-2]: confirmation バー
    candles[-1]: エントリーバー（現在の未確定足）

    シグナルがなければ None を返す。
    spread/slippage はここでは加味しない（呼び出し元で加算）。
    """
    min_len = max(bb_period, adx_period * 2 + 1, rsi_period + 1) + 3
    if len(candles) < min_len:
        return None

    pip = 0.01 if pair.endswith("JPY") else 0.0001

    setup_bar   = candles[-3]
    confirm_bar = candles[-2]
    entry_bar   = candles[-1]

    # 指標は setup バー時点（candles[:-2]）で計算
    setup_data = candles[:-2]
    closes = [c.close for c in setup_data]

    bb  = calc_bb(closes, bb_period)
    rsi = calc_rsi(closes, rsi_period)
    adx = calc_adx(setup_data, adx_period)

    if bb is None or rsi is None or adx is None:
        return None

    bb_upper, bb_middle, bb_lower = bb

    direction = None

    if (adx < adx_threshold
            and rsi <= rsi_os
            and setup_bar.low <= bb_lower
            and confirm_bar.close > confirm_bar.open):
        direction = "BUY"

    elif (adx < adx_threshold
          and rsi >= rsi_ob
          and setup_bar.high >= bb_upper
          and confirm_bar.close < confirm_bar.open):
        direction = "SELL"

    if direction is None:
        return None

    entry = entry_bar.open
    sl_buf = sl_buffer_pips * pip

    if direction == "BUY":
        sl = bb_lower - sl_buf
        tp = bb_middle
    else:
        sl = bb_upper + sl_buf
        tp = bb_middle

    if direction == "BUY":
        sl_d = entry - sl   # sl must be below entry
        tp_d = tp - entry   # tp must be above entry
    else:
        sl_d = sl - entry   # sl must be above entry
        tp_d = entry - tp   # tp must be below entry

    if sl_d <= 0 or tp_d <= 0 or tp_d / sl_d < rr_min:
        return None

    return RangeSignal(
        direction=direction,
        entry_price=round(entry, 5),
        sl_price=round(sl, 5),
        tp_price=round(tp, 5),
        rr=round(tp_d / sl_d, 2),
        setup_time=setup_bar.time.isoformat(),
        confirm_time=confirm_bar.time.isoformat(),
    )


# ------------------------------------------------------------------
# 内部ヘルパー
# ------------------------------------------------------------------

def _rsi_from_avgs(avg_g: float, avg_l: float) -> float:
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def _dx(atr: float, pdm: float, mdm: float) -> float:
    if atr == 0:
        return 0.0
    plus_di  = 100 * pdm / atr
    minus_di = 100 * mdm / atr
    di_sum   = plus_di + minus_di
    return 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
