"""
バックテスト - Case 1: BB レンジ逆張り  (ver0.2)

仕様 ver0.2 に基づき、フィルターを config で切り替えてグリッド比較する。

使い方:
  python scripts/backtest.py                        # 両ペア H1 グリッド比較
  python scripts/backtest.py --pair EUR_USD --tf M5 # EUR/USD M5
  python scripts/backtest.py --no-cache             # データ再取得

グリッドで比較される設定（spec ver0.2）:
  1. ADX < 20  (ベースライン)
  2. ADX < 25
  3. ADX < 30
  4. ADX フィルターなし
  5. ADX < 25 + ヒステリシス (停止 > 30)
  6. ADX < 25 + BB幅急拡大フィルター
  7. ADX < 25 + MA75 傾きフィルター
  8. ADX < 25 + 時間帯フィルター (東京 / ロンドン)
  9. ADX < 25 + 全フィルター組み合わせ
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR    = ROOT / "data" / "backtest_cache"
RESULTS_FILE = ROOT / "data" / "backtest_results.json"

SPREAD_PIPS: dict[str, float] = {
    "USD_JPY": 0.7, "EUR_USD": 0.7, "GBP_USD": 1.0, "AUD_USD": 0.8,
    "AUD_JPY": 0.7, "GBP_JPY": 1.2, "EUR_JPY": 0.8, "EUR_GBP": 0.8,
}
_TF_MAX_CANDLES = {"M5": 17_000, "H1": 17_500}
_AV_CACHE_DIR = ROOT / "data" / "backtest_cache" / "av"


# ------------------------------------------------------------------
# 設定データクラス
# ------------------------------------------------------------------

@dataclass
class BacktestConfig:
    label: str

    # ADX フィルター（ヒステリシス付き）
    adx_enter: float | None = 20.0   # None = フィルターなし
    adx_exit:  float | None = None   # None = adx_enter と同値（ヒステリシスなし）

    # BB幅フィルター: 直近 bb_width_window 本の平均幅の倍率を超えたら停止
    bb_width_mult:   float | None = None  # e.g. 1.5
    bb_width_window: int = 20

    # MA75傾きフィルター: 直近 ma_slope_bars 本の変化が ±slope_pips 以内ならレンジ
    ma_slope_pips: float | None = None   # e.g. 2.0
    ma_slope_bars: int = 10

    # 時間帯フィルター: 指定セッション以外はエントリーしない
    sessions: list[str] | None = None   # e.g. ["TOKYO", "LONDON"]

    # リスク/エントリー
    rsi_ob:       float = 70.0
    rsi_os:       float = 30.0
    sl_buf_pips:  float = 2.0
    rr_min:       float = 2.0
    spread_pips:  float = 0.7
    max_daily_losses: int = 2

    # タッチエントリーモード: 確認足なし、エントリー = バンドレベル
    touch_entry:  bool  = False


@dataclass
class TrendConfig:
    label: str
    adx_min:       float | None = 25.0  # None = ADX フィルターなし
    slope_pips:    float = 2.0          # MA75 が N pips/slope_bars 以上動いていること
    slope_bars:    int   = 10
    sl_buf_pips:   float = 5.0
    rr_min:        float = 2.0
    spread_pips:   float = 0.7
    max_daily_losses: int = 2


# ------------------------------------------------------------------
# データ取得（yfinance・キャッシュあり）
# ------------------------------------------------------------------

def _cache_path(pair: str, tf: str) -> Path:
    return CACHE_DIR / f"{pair}_{tf}_yf.json"


def _av_merged_cache_path(pair: str, tf: str, months: int) -> Path:
    return CACHE_DIR / f"{pair}_{tf}_av_{months}m.json"


def load_or_fetch(pair: str, tf: str = "H1") -> list:
    """yfinance からデータ取得（キャッシュあり）"""
    from src.data.oanda_client import Candle

    cache = _cache_path(pair, tf)
    if cache.exists():
        logger.info("キャッシュから読み込み: %s", cache.name)
        raw = json.loads(cache.read_text(encoding="utf-8"))
        return [
            Candle(
                time=datetime.fromisoformat(r["t"]),
                open=r["o"], high=r["h"], low=r["l"], close=r["c"],
                volume=r.get("v", 0),
            )
            for r in raw
        ]

    logger.info("yfinance から取得: %s %s", pair, tf)
    from src.data.yfinance_client import YFinanceClient
    client = YFinanceClient()
    count = _TF_MAX_CANDLES.get(tf, 17_500)
    candles = client.get_candles(pair, tf, count=count)
    logger.info("取得完了: %d 本", len(candles))

    if not candles:
        raise RuntimeError(f"データ取得失敗: {pair} {tf}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps([
            {"t": c.time.isoformat(), "o": c.open, "h": c.high,
             "l": c.low, "c": c.close, "v": c.volume}
            for c in candles
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    return candles


def load_or_fetch_av(pair: str, tf: str, months: int, api_key: str) -> list:
    """
    Alpha Vantage からデータ取得。

    月ごとにキャッシュ（av/ サブディレクトリ）するため、
    取得が途中で止まっても翌日再実行で続きから取れる。

    無料プラン: 25 リクエスト/日。2年分 = 24 ヶ月/ペア。
    2ペア同時に取ろうとすると 48 リクエスト = 2日かかる。
    """
    from src.data.alpha_vantage_client import fetch_range

    merged_cache = _av_merged_cache_path(pair, tf, months)
    if merged_cache.exists():
        logger.info("AVマージキャッシュ読み込み: %s", merged_cache.name)
        from src.data.oanda_client import Candle
        raw = json.loads(merged_cache.read_text(encoding="utf-8"))
        return [
            Candle(
                time=datetime.fromisoformat(r["t"]),
                open=r["o"], high=r["h"], low=r["l"], close=r["c"],
                volume=0,
            )
            for r in raw
        ]

    logger.info("Alpha Vantage から取得: %s %s %d ヶ月分", pair, tf, months)
    candles = fetch_range(pair, tf, months, api_key, _AV_CACHE_DIR)
    logger.info("取得完了: %d 本", len(candles))

    if not candles:
        raise RuntimeError(f"AV データ取得失敗: {pair} {tf}")

    # 月別キャッシュが揃ったらマージキャッシュも保存
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    merged_cache.write_text(
        json.dumps([
            {"t": c.time.isoformat(), "o": c.open, "h": c.high,
             "l": c.low, "c": c.close}
            for c in candles
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    return candles


# ------------------------------------------------------------------
# 指標一括計算 O(n)
# ------------------------------------------------------------------

def precompute(candles, bb_period=20, rsi_period=14, adx_period=14, ma_period=75):
    """BB / RSI / ADX / MA75 を一括計算して配列で返す"""
    n = len(candles)
    bb_up  = [None] * n
    bb_mid = [None] * n
    bb_lo  = [None] * n
    rsi_v  = [None] * n
    adx_v  = [None] * n
    ma75_v = [None] * n

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]

    # ---- Bollinger Bands ----
    for i in range(bb_period - 1, n):
        w = closes[i - bb_period + 1 : i + 1]
        m = sum(w) / bb_period
        v = sum((x - m) ** 2 for x in w) / bb_period
        s = math.sqrt(v)
        bb_up[i]  = m + 2 * s
        bb_mid[i] = m
        bb_lo[i]  = m - 2 * s

    # ---- MA75 ----
    for i in range(ma_period - 1, n):
        ma75_v[i] = sum(closes[i - ma_period + 1 : i + 1]) / ma_period

    # ---- RSI: Wilder's ----
    if n > rsi_period:
        diffs  = [closes[i] - closes[i - 1] for i in range(1, n)]
        gains  = [max(d, 0.0) for d in diffs]
        losses = [max(-d, 0.0) for d in diffs]

        avg_g = sum(gains[:rsi_period])  / rsi_period
        avg_l = sum(losses[:rsi_period]) / rsi_period
        rsi_v[rsi_period] = _rsi(avg_g, avg_l)

        for i in range(rsi_period + 1, n):
            j = i - 1
            avg_g = (avg_g * (rsi_period - 1) + gains[j])  / rsi_period
            avg_l = (avg_l * (rsi_period - 1) + losses[j]) / rsi_period
            rsi_v[i] = _rsi(avg_g, avg_l)

    # ---- ADX: 2段階 Wilder's ----
    if n >= adx_period * 2 + 1:
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

        s_tr  = sum(trs[:adx_period])  / adx_period
        s_pdm = sum(pdms[:adx_period]) / adx_period
        s_mdm = sum(mdms[:adx_period]) / adx_period

        dx_series: list[tuple[int, float]] = [(adx_period, _dx(s_tr, s_pdm, s_mdm))]
        for j in range(adx_period, len(trs)):
            s_tr  = (s_tr  * (adx_period - 1) + trs[j])  / adx_period
            s_pdm = (s_pdm * (adx_period - 1) + pdms[j]) / adx_period
            s_mdm = (s_mdm * (adx_period - 1) + mdms[j]) / adx_period
            dx_series.append((j + 1, _dx(s_tr, s_pdm, s_mdm)))

        if len(dx_series) >= adx_period:
            adx_val = sum(dx for _, dx in dx_series[:adx_period]) / adx_period
            adx_v[dx_series[adx_period - 1][0]] = adx_val
            for k in range(adx_period, len(dx_series)):
                cidx, dx = dx_series[k]
                adx_val = (adx_val * (adx_period - 1) + dx) / adx_period
                adx_v[cidx] = adx_val

    return bb_up, bb_mid, bb_lo, rsi_v, adx_v, ma75_v


# ------------------------------------------------------------------
# トレンド戦略 事前計算 / シミュレーション
# ------------------------------------------------------------------

def precompute_trend(candles, ma_fast=5, ma_mid=20, ma_slow=75, adx_period=14):
    """MA5 / MA20 / MA75 / ADX を一括計算"""
    n = len(candles)
    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]

    def _ma(period):
        arr = [None] * n
        for i in range(period - 1, n):
            arr[i] = sum(closes[i - period + 1 : i + 1]) / period
        return arr

    mf_v  = _ma(ma_fast)
    mm_v  = _ma(ma_mid)
    ms_v  = _ma(ma_slow)

    # ADX（BB 戦略と同じ 2 段階 Wilder's）
    adx_v = [None] * n
    if n >= adx_period * 2 + 1:
        trs, pdms, mdms = [], [], []
        for i in range(1, n):
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            up   = highs[i] - highs[i-1]
            down = lows[i-1] - lows[i]
            trs.append(tr)
            pdms.append(up   if (up   > down and up   > 0) else 0.0)
            mdms.append(down if (down > up   and down > 0) else 0.0)

        s_tr  = sum(trs[:adx_period])  / adx_period
        s_pdm = sum(pdms[:adx_period]) / adx_period
        s_mdm = sum(mdms[:adx_period]) / adx_period
        dx_series = [(adx_period, _dx(s_tr, s_pdm, s_mdm))]
        for j in range(adx_period, len(trs)):
            s_tr  = (s_tr  * (adx_period-1) + trs[j])  / adx_period
            s_pdm = (s_pdm * (adx_period-1) + pdms[j]) / adx_period
            s_mdm = (s_mdm * (adx_period-1) + mdms[j]) / adx_period
            dx_series.append((j+1, _dx(s_tr, s_pdm, s_mdm)))
        if len(dx_series) >= adx_period:
            adx_val = sum(dx for _, dx in dx_series[:adx_period]) / adx_period
            adx_v[dx_series[adx_period-1][0]] = adx_val
            for k in range(adx_period, len(dx_series)):
                cidx, dx = dx_series[k]
                adx_val = (adx_val * (adx_period-1) + dx) / adx_period
                adx_v[cidx] = adx_val

    return mf_v, mm_v, ms_v, adx_v


def simulate_trend(candles, pair, mf_v, mm_v, ms_v, adx_v, cfg: TrendConfig) -> list[dict]:
    pip    = 0.01 if pair.endswith("JPY") else 0.0001
    cost   = cfg.spread_pips * pip
    sl_buf = cfg.sl_buf_pips * pip

    trades: list[dict] = []
    in_trade = False
    trade: dict = {}
    daily_losses: dict[str, int] = {}

    def jst_date(dt) -> str:
        return (dt + timedelta(hours=9)).strftime("%Y-%m-%d")

    n = len(candles)
    for i in range(2, n):
        c   = candles[i]
        day = jst_date(c.time)

        # ---- 決済チェック ----
        if in_trade:
            result = exit_price = None
            if trade["dir"] == "BUY":
                if c.low  <= trade["sl"]: result, exit_price = "SL", trade["sl"]
                elif c.high >= trade["tp"]: result, exit_price = "TP", trade["tp"]
            else:
                if c.high >= trade["sl"]: result, exit_price = "SL", trade["sl"]
                elif c.low  <= trade["tp"]: result, exit_price = "TP", trade["tp"]

            h = (c.time + timedelta(hours=9)).hour
            m = (c.time + timedelta(hours=9)).minute
            if result is None and h == 23 and m >= 55:
                result, exit_price = "EOD", c.close

            if result:
                pips = (exit_price - trade["entry"]) / pip if trade["dir"] == "BUY" \
                       else (trade["entry"] - exit_price) / pip
                entry_day = jst_date(candles[trade["bar"]].time)
                if result == "SL":
                    daily_losses[entry_day] = daily_losses.get(entry_day, 0) + 1
                trades.append({
                    "entry_time": candles[trade["bar"]].time.isoformat(),
                    "exit_time":  c.time.isoformat(),
                    "pair": pair, "direction": trade["dir"],
                    "entry": trade["entry"], "exit": exit_price,
                    "sl": trade["sl"], "tp": trade["tp"],
                    "result": result, "pips": round(pips, 1),
                    "session": _session(candles[trade["bar"]].time),
                })
                in_trade = False
            continue

        if daily_losses.get(day, 0) >= cfg.max_daily_losses:
            continue

        i_s = i - 2
        i_c = i - 1

        if any(v is None for v in [mf_v[i_s], mm_v[i_s], ms_v[i_s]]):
            continue

        # ADX フィルター
        if cfg.adx_min is not None:
            if adx_v[i_s] is None or adx_v[i_s] < cfg.adx_min:
                continue

        mf, mm, ms = mf_v[i_s], mm_v[i_s], ms_v[i_s]
        setup   = candles[i_s]
        confirm = candles[i_c]
        entry_c = candles[i]

        # MA75 傾き
        j_prev = i_s - cfg.slope_bars
        if j_prev < 0 or ms_v[j_prev] is None:
            continue
        slope = (ms - ms_v[j_prev]) / pip

        direction = None
        if (mf > mm > ms
                and slope >= cfg.slope_pips
                and setup.low  <= mm          # MA20 まで押した
                and setup.low  >= ms - sl_buf  # MA75 を大きく割っていない
                and confirm.close > confirm.open  # 陽線
                and confirm.close >= mm):          # MA20 を回復
            direction = "BUY"
        elif (mf < mm < ms
                and slope <= -cfg.slope_pips
                and setup.high >= mm
                and setup.high <= ms + sl_buf
                and confirm.close < confirm.open
                and confirm.close <= mm):
            direction = "SELL"

        if direction is None:
            continue

        if direction == "BUY":
            entry = entry_c.open + cost
            sl    = setup.low - sl_buf   # 押し目の直下
            sl_d  = entry - sl
            tp    = entry + cfg.rr_min * sl_d
        else:
            entry = entry_c.open - cost
            sl    = setup.high + sl_buf  # 戻り高値の直上
            sl_d  = sl - entry
            tp    = entry - cfg.rr_min * sl_d

        if sl_d <= 0:
            continue

        in_trade = True
        trade = {"dir": direction, "entry": round(entry,5),
                 "sl": round(sl,5), "tp": round(tp,5), "bar": i}

    return trades


def run_trend_grid(candles, pair: str, configs: list[TrendConfig]) -> list[dict]:
    mf_v, mm_v, ms_v, adx_v = precompute_trend(candles)

    t_start = candles[0].time.strftime("%Y-%m-%d")
    t_end   = candles[-1].time.strftime("%Y-%m-%d")
    split   = int(len(candles) * 0.6)

    print(f"\n{'#' * 66}")
    print(f"# {pair} [TREND]  |  {t_start} 〜 {t_end}  ({len(candles):,} 本)")
    print(f"{'#' * 66}")
    print(f"  ウォークフォワード: 前半60% インサンプル / 後半40% OOS")

    rows = []
    for cfg in configs:
        tr_all = simulate_trend(candles, pair, mf_v, mm_v, ms_v, adx_v, cfg)
        b2 = [mf_v[:split], mm_v[:split], ms_v[:split], adx_v[:split]]
        tr_in  = simulate_trend(candles[:split], pair, *b2, cfg)
        b3 = [mf_v[split:], mm_v[split:], ms_v[split:], adx_v[split:]]
        tr_out = simulate_trend(candles[split:], pair, *b3, cfg)

        rows.append({"cfg": cfg, "all": calc_stats(tr_all),
                     "in": calc_stats(tr_in), "out": calc_stats(tr_out),
                     "trades": tr_all})

    _print_grid_table(rows)
    return rows


def default_trend_grid(spread_pips: float) -> list[TrendConfig]:
    kw = {"spread_pips": spread_pips}
    return [
        TrendConfig("ADXなし  RR2",   adx_min=None, rr_min=2.0, **kw),
        TrendConfig("ADX>20  RR2",    adx_min=20.0, rr_min=2.0, **kw),
        TrendConfig("ADX>25  RR2",    adx_min=25.0, rr_min=2.0, **kw),
        TrendConfig("ADX>30  RR2",    adx_min=30.0, rr_min=2.0, **kw),
        TrendConfig("ADX>25  RR3",    adx_min=25.0, rr_min=3.0, **kw),
        TrendConfig("ADX>25  SL10",   adx_min=25.0, sl_buf_pips=10.0, rr_min=2.0, **kw),
        TrendConfig("ADX>25  slope5", adx_min=25.0, slope_pips=5.0, rr_min=2.0, **kw),
    ]


# ------------------------------------------------------------------
# BB幅の事前計算（急拡大フィルター用）
# ------------------------------------------------------------------

def _bb_width_array(bb_up: list, bb_lo: list) -> list[float | None]:
    return [
        u - l if (u is not None and l is not None) else None
        for u, l in zip(bb_up, bb_lo)
    ]


def _bb_width_expanding(bb_width: list, i: int, window: int, mult: float) -> bool:
    """BB幅が直近 window 本の平均の mult 倍を超えていたら True（急拡大）"""
    if bb_width[i] is None:
        return False
    past = [bb_width[j] for j in range(max(0, i - window), i) if bb_width[j] is not None]
    if len(past) < window // 2:
        return False
    avg = sum(past) / len(past)
    return avg > 0 and bb_width[i] > avg * mult


# ------------------------------------------------------------------
# シミュレーション
# ------------------------------------------------------------------

def simulate(
    candles,
    pair: str,
    bb_up, bb_mid, bb_lo, rsi_v, adx_v, ma75_v,
    cfg: BacktestConfig,
) -> list[dict]:
    pip    = 0.01 if pair.endswith("JPY") else 0.0001
    cost   = cfg.spread_pips * pip
    sl_buf = cfg.sl_buf_pips * pip

    adx_exit_thr = cfg.adx_exit if cfg.adx_exit is not None else cfg.adx_enter
    bb_width = _bb_width_array(bb_up, bb_lo)

    trades: list[dict] = []
    in_trade   = False
    trade: dict = {}
    in_range   = False   # ヒステリシス用状態

    daily_losses: dict[str, int] = {}

    def jst_date(dt: datetime) -> str:
        return (dt + timedelta(hours=9)).strftime("%Y-%m-%d")

    def jst_hm(dt: datetime) -> tuple[int, int]:
        t = dt + timedelta(hours=9)
        return t.hour, t.minute

    n = len(candles)

    for i in range(2, n):
        c   = candles[i]
        day = jst_date(c.time)

        # ---- 決済チェック ----
        if in_trade:
            result = exit_price = None

            if trade["dir"] == "BUY":
                if c.low <= trade["sl"]:
                    result, exit_price = "SL", trade["sl"]
                elif c.high >= trade["tp"]:
                    result, exit_price = "TP", trade["tp"]
            else:
                if c.high >= trade["sl"]:
                    result, exit_price = "SL", trade["sl"]
                elif c.low <= trade["tp"]:
                    result, exit_price = "TP", trade["tp"]

            h, m = jst_hm(c.time)
            if result is None and h == 23 and m >= 55:
                result, exit_price = "EOD", c.close

            if result:
                if trade["dir"] == "BUY":
                    pips = (exit_price - trade["entry"]) / pip
                else:
                    pips = (trade["entry"] - exit_price) / pip

                entry_day = jst_date(candles[trade["bar"]].time)
                if result == "SL":
                    daily_losses[entry_day] = daily_losses.get(entry_day, 0) + 1

                trades.append({
                    "entry_time": candles[trade["bar"]].time.isoformat(),
                    "exit_time":  c.time.isoformat(),
                    "pair":       pair,
                    "direction":  trade["dir"],
                    "entry":      trade["entry"],
                    "exit":       exit_price,
                    "sl":         trade["sl"],
                    "tp":         trade["tp"],
                    "result":     result,
                    "pips":       round(pips, 1),
                    "session":    _session(candles[trade["bar"]].time),
                })
                in_trade = False
            continue

        # ---- 当日連敗上限 ----
        if daily_losses.get(day, 0) >= cfg.max_daily_losses:
            continue

        # ---- ADX ヒステリシス状態管理 ----
        adx_ref = i - 1 if cfg.touch_entry else i - 2
        if cfg.adx_enter is not None:
            adx_now = adx_v[adx_ref]
            if adx_now is None:
                continue
            if not in_range and adx_now < cfg.adx_enter:
                in_range = True
            elif in_range and adx_exit_thr is not None and adx_now > adx_exit_thr:
                in_range = False
            if not in_range:
                continue

        # ---- シグナル検出 ----
        if cfg.touch_entry:
            # タッチエントリー: setup = 前足、確認足なし、エントリー = バンドレベル
            i_s = i - 1
        else:
            i_s = i - 2   # setup バー
        i_c = i - 1   # confirm バー（touch_entry 時は使わない）

        if bb_up[i_s] is None or rsi_v[i_s] is None:
            continue

        # ADX フィルターなし の場合でも None チェック
        if cfg.adx_enter is not None and adx_v[i_s] is None:
            continue

        # ---- BB幅急拡大フィルター ----
        if cfg.bb_width_mult is not None:
            if _bb_width_expanding(bb_width, i_s, cfg.bb_width_window, cfg.bb_width_mult):
                continue

        # ---- MA75 傾きフィルター ----
        if cfg.ma_slope_pips is not None:
            slope_bars = cfg.ma_slope_bars
            j_prev = i_s - slope_bars
            if j_prev >= 0 and ma75_v[i_s] is not None and ma75_v[j_prev] is not None:
                slope_pips = abs(ma75_v[i_s] - ma75_v[j_prev]) / pip
                if slope_pips > cfg.ma_slope_pips:
                    continue
            else:
                continue

        setup   = candles[i_s]
        entry_c = candles[i]

        # ---- 時間帯フィルター ----
        if cfg.sessions is not None:
            if _session(entry_c.time) not in cfg.sessions:
                continue

        direction = None
        if cfg.touch_entry:
            # 確認足なし: バンドタッチのみで判定
            if rsi_v[i_s] <= cfg.rsi_os and setup.low <= bb_lo[i_s]:
                direction = "BUY"
            elif rsi_v[i_s] >= cfg.rsi_ob and setup.high >= bb_up[i_s]:
                direction = "SELL"
        else:
            confirm = candles[i_c]
            if (rsi_v[i_s] <= cfg.rsi_os
                    and setup.low <= bb_lo[i_s]
                    and confirm.close > confirm.open):
                direction = "BUY"
            elif (rsi_v[i_s] >= cfg.rsi_ob
                  and setup.high >= bb_up[i_s]
                  and confirm.close < confirm.open):
                direction = "SELL"

        if direction is None:
            continue

        if direction == "BUY":
            # タッチエントリー時はバンドレベルで即エントリー
            entry = (bb_lo[i_s] + cost) if cfg.touch_entry else (entry_c.open + cost)
            sl    = bb_lo[i_s] - sl_buf
            tp    = bb_mid[i_s]
            sl_d  = entry - sl
            tp_d  = tp - entry
        else:
            entry = (bb_up[i_s] - cost) if cfg.touch_entry else (entry_c.open - cost)
            sl    = bb_up[i_s] + sl_buf
            tp    = bb_mid[i_s]
            sl_d  = sl - entry
            tp_d  = entry - tp

        if sl_d <= 0 or tp_d <= 0 or tp_d / sl_d < cfg.rr_min:
            continue

        in_trade = True
        trade = {
            "dir":   direction,
            "entry": round(entry, 5),
            "sl":    round(sl, 5),
            "tp":    round(tp, 5),
            "bar":   i,
        }

    return trades


# ------------------------------------------------------------------
# 統計計算
# ------------------------------------------------------------------

def calc_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"total": 0, "win_rate": 0, "net_pips": 0,
                "ev": 0, "pf": 0, "max_dd": 0, "max_con_loss": 0,
                "avg_win": 0, "avg_loss": 0}

    wins   = [t for t in trades if t["result"] == "TP"]
    losses = [t for t in trades if t["result"] == "SL"]

    total    = len(trades)
    net_pips = sum(t["pips"] for t in trades)
    avg_win  = sum(t["pips"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(t["pips"] for t in losses) / len(losses) if losses else 0.0

    gross_win  = sum(t["pips"] for t in wins)
    gross_loss = abs(sum(t["pips"] for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    cum = peak = max_dd = 0.0
    for t in trades:
        cum += t["pips"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    max_con = con = 0
    for t in trades:
        if t["result"] == "SL":
            con += 1
            max_con = max(max_con, con)
        else:
            con = 0

    return {
        "total":        total,
        "win_rate":     len(wins) / total * 100,
        "net_pips":     net_pips,
        "ev":           net_pips / total,
        "pf":           pf,
        "max_dd":       max_dd,
        "max_con_loss": max_con,
        "avg_win":      avg_win,
        "avg_loss":     avg_loss,
    }


# ------------------------------------------------------------------
# グリッド比較
# ------------------------------------------------------------------

def run_grid(candles, pair: str, configs: list[BacktestConfig]) -> list[dict]:
    """複数設定を同じデータで回して比較表を出力する"""
    bb_up, bb_mid, bb_lo, rsi_v, adx_v, ma75_v = precompute(candles)

    t_start = candles[0].time.strftime("%Y-%m-%d")
    t_end   = candles[-1].time.strftime("%Y-%m-%d")
    print(f"\n{'#' * 66}")
    print(f"# {pair}  |  {t_start} 〜 {t_end}  ({len(candles):,} 本)")
    print(f"{'#' * 66}")
    print(f"  ウォークフォワード: 前半60% インサンプル / 後半40% OOS")

    split = int(len(candles) * 0.6)
    rows  = []

    for cfg in configs:
        # 全期間
        tr_all = simulate(candles, pair, bb_up, bb_mid, bb_lo, rsi_v, adx_v, ma75_v, cfg)
        # In-sample
        bb2 = [bb_up[:split], bb_mid[:split], bb_lo[:split],
               rsi_v[:split], adx_v[:split], ma75_v[:split]]
        tr_in = simulate(candles[:split], pair, *bb2, cfg)
        # OOS
        bb3 = [bb_up[split:], bb_mid[split:], bb_lo[split:],
               rsi_v[split:], adx_v[split:], ma75_v[split:]]
        tr_out = simulate(candles[split:], pair, *bb3, cfg)

        s_all = calc_stats(tr_all)
        s_in  = calc_stats(tr_in)
        s_out = calc_stats(tr_out)

        rows.append({
            "cfg":   cfg,
            "all":   s_all,
            "in":    s_in,
            "out":   s_out,
            "trades": tr_all,
        })

    # ---- 比較テーブル出力 ----
    _print_grid_table(rows)
    return rows


def _print_grid_table(rows: list[dict]) -> None:
    h1 = f"{'設定':<30}"
    h2 = f"{'回数':>4}  {'勝率':>6}  {'純pips':>8}  {'EV':>6}  {'PF':>5}  {'MaxDD':>7}"
    print(f"\n  {'全期間':^{len(h2)}}")
    print(f"  {h1}  {h2}")
    print(f"  {'-' * (len(h1) + len(h2) + 2)}")

    for row in rows:
        s = row["all"]
        label = row["cfg"].label[:30]
        inf_mark = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(
            f"  {label:<30}  "
            f"{s['total']:>4}  "
            f"{s['win_rate']:>5.1f}%  "
            f"{s['net_pips']:>+8.1f}  "
            f"{s['ev']:>+6.2f}  "
            f"{inf_mark:>5}  "
            f"{-s['max_dd']:>+7.1f}"
        )

    # OOS のみ追加比較
    print(f"\n  {'OOS（後半40%）':^{len(h2)}}")
    print(f"  {h1}  {h2}")
    print(f"  {'-' * (len(h1) + len(h2) + 2)}")

    for row in rows:
        s = row["out"]
        if s["total"] == 0:
            print(f"  {row['cfg'].label:<30}  {'(なし)':>{len(h2)}}")
            continue
        label = row["cfg"].label[:30]
        inf_mark = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(
            f"  {label:<30}  "
            f"{s['total']:>4}  "
            f"{s['win_rate']:>5.1f}%  "
            f"{s['net_pips']:>+8.1f}  "
            f"{s['ev']:>+6.2f}  "
            f"{inf_mark:>5}  "
            f"{-s['max_dd']:>+7.1f}"
        )


# ------------------------------------------------------------------
# レポート（詳細表示）
# ------------------------------------------------------------------

def print_report(trades: list[dict], label: str = "") -> None:
    s = calc_stats(trades)
    if s["total"] == 0:
        print(f"\n  {label}: トレードなし")
        return

    wins   = [t for t in trades if t["result"] == "TP"]
    losses = [t for t in trades if t["result"] == "SL"]
    eod    = [t for t in trades if t["result"] == "EOD"]

    print(f"\n{'=' * 50}")
    print(f"  {label}")
    print(f"{'=' * 50}")
    print(f"  総トレード    : {s['total']}")
    print(f"  勝率          : {s['win_rate']:.1f}%  ({len(wins)}勝 / {len(losses)}敗 / {len(eod)}EOD)")
    print(f"  純損益        : {s['net_pips']:+.1f} pips")
    print(f"  期待値        : {s['ev']:+.2f} pips/トレード")
    print(f"  平均利益      : {s['avg_win']:+.1f} pips")
    print(f"  平均損失      : {s['avg_loss']:+.1f} pips")
    print(f"  PF            : {s['pf']:.2f}" if s['pf'] != float('inf') else "  PF            : ∞")
    print(f"  最大連敗      : {s['max_con_loss']}")
    print(f"  最大DD        : -{s['max_dd']:.1f} pips")

    sessions: dict[str, list] = {}
    for t in trades:
        sessions.setdefault(t.get("session", "OTHER"), []).append(t)

    print(f"\n  【セッション別 (JST)】")
    for sname in ("TOKYO", "LONDON", "NY", "OTHER"):
        ss = sessions.get(sname, [])
        if not ss:
            continue
        sw = [x for x in ss if x["result"] == "TP"]
        print(
            f"    {sname:<8}: {len(ss):>3} トレード  "
            f"勝率 {len(sw)/len(ss)*100:4.1f}%  "
            f"計 {sum(x['pips'] for x in ss):+.1f} pips"
        )

    # 月次損益
    monthly: dict[str, list] = {}
    for t in trades:
        ym = t["entry_time"][:7]  # "YYYY-MM"
        monthly.setdefault(ym, []).append(t)

    print(f"\n  【月次損益】")
    print(f"    {'月':<8}  {'回数':>4}  {'勝率':>6}  {'pips':>8}")
    print(f"    {'-' * 32}")
    total_monthly = 0.0
    for ym in sorted(monthly):
        mt = monthly[ym]
        mw = [x for x in mt if x["result"] == "TP"]
        mp = sum(x["pips"] for x in mt)
        total_monthly += mp
        print(
            f"    {ym}  {len(mt):>4}  "
            f"{len(mw)/len(mt)*100:>5.1f}%  "
            f"{mp:>+8.1f}"
        )
    print(f"    {'合計':<8}  {'':>4}  {'':>6}  {total_monthly:>+8.1f}")


# ------------------------------------------------------------------
# デフォルトグリッド設定（spec ver0.2）
# ------------------------------------------------------------------

def default_grid(spread_pips: float) -> list[BacktestConfig]:
    kw = {"spread_pips": spread_pips}
    return [
        BacktestConfig("ADX<20 (ベースライン)",    adx_enter=20.0, **kw),
        BacktestConfig("ADX<25",                   adx_enter=25.0, **kw),
        BacktestConfig("ADX<30",                   adx_enter=30.0, **kw),
        BacktestConfig("ADXなし",                  adx_enter=None, **kw),
        BacktestConfig("ADX<25+ヒステリシス(>30)", adx_enter=25.0, adx_exit=30.0, **kw),
        BacktestConfig("ADX<25+BB幅急拡大フィルター", adx_enter=25.0, bb_width_mult=1.5, **kw),
        BacktestConfig("ADX<25+MA75傾き",          adx_enter=25.0, ma_slope_pips=2.0, **kw),
        BacktestConfig("ADX<25+東京+ロンドン",     adx_enter=25.0,
                       sessions=["TOKYO", "LONDON"], **kw),
        BacktestConfig("ADX<25+全フィルター",      adx_enter=25.0, adx_exit=30.0,
                       bb_width_mult=1.5, ma_slope_pips=2.0,
                       sessions=["TOKYO", "LONDON"], **kw),
    ]


def adx_sweep_grid(spread_pips: float) -> list[BacktestConfig]:
    """ADX 閾値を細かく刻んで最適値を探すグリッド"""
    kw = {"spread_pips": spread_pips}
    configs = []
    for thr in [15, 18, 20, 22, 25, 27, 30, 33, 35, 40, 45, 50]:
        configs.append(BacktestConfig(f"ADX<{thr}", adx_enter=float(thr), **kw))
    configs.append(BacktestConfig("ADXなし", adx_enter=None, **kw))
    return configs


def best_config(spread_pips: float) -> list[BacktestConfig]:
    """RSI 35/65 + ADX<30 固定（最適設定の単一確認用）"""
    return [BacktestConfig("RSI35/65 ADX<30", adx_enter=30.0, rsi_os=35.0, rsi_ob=65.0, spread_pips=spread_pips)]


def rsi_sweep_grid(spread_pips: float, adx_enter: float = 30.0) -> list[BacktestConfig]:
    """RSI 閾値を刻んで最適値を探すグリッド（ADX は固定）"""
    kw = {"spread_pips": spread_pips, "adx_enter": adx_enter}
    configs = []
    for os_thr in [25, 28, 30, 33, 35, 38, 40, 45]:
        ob_thr = 100 - os_thr
        configs.append(BacktestConfig(
            f"RSI {os_thr}/{ob_thr}  (ADX<{adx_enter:.0f})",
            rsi_os=float(os_thr), rsi_ob=float(ob_thr), **kw,
        ))
    return configs


def sl_sweep_grid(spread_pips: float, adx_enter: float = 30.0) -> list[BacktestConfig]:
    """SL バッファ幅を刻んで最適値を探すグリッド（ADX<30, RSI35/65 固定）"""
    kw = {"spread_pips": spread_pips, "adx_enter": adx_enter,
          "rsi_os": 35.0, "rsi_ob": 65.0}
    configs = []
    for sl in [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0]:
        configs.append(BacktestConfig(
            f"SL {sl:.0f}pips  (ADX<{adx_enter:.0f})",
            sl_buf_pips=sl, **kw,
        ))
    return configs


def sl_touch_grid(spread_pips: float, adx_enter: float = 30.0) -> list[BacktestConfig]:
    """タッチエントリーモードで SL バッファ幅を刻むグリッド（確認足なし）"""
    kw = {"spread_pips": spread_pips, "adx_enter": adx_enter,
          "rsi_os": 35.0, "rsi_ob": 65.0, "touch_entry": True}
    configs = []
    for sl in [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0]:
        configs.append(BacktestConfig(
            f"TOUCH SL {sl:.0f}pips  (ADX<{adx_enter:.0f})",
            sl_buf_pips=sl, **kw,
        ))
    return configs


# ------------------------------------------------------------------
# 内部ヘルパー
# ------------------------------------------------------------------

def _rsi(avg_g: float, avg_l: float) -> float:
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def _dx(atr: float, pdm: float, mdm: float) -> float:
    if atr == 0:
        return 0.0
    pd = 100 * pdm / atr; md = 100 * mdm / atr
    s  = pd + md
    return 100 * abs(pd - md) / s if s > 0 else 0.0


def _session(dt: datetime) -> str:
    h = (dt.hour + 9) % 24
    if 9 <= h < 15:  return "TOKYO"
    if 15 <= h < 21: return "LONDON"
    if 21 <= h or h < 3: return "NY"
    return "OTHER"


# ------------------------------------------------------------------
# エントリーポイント
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="BB レンジ逆張りバックテスト ver0.2")
    parser.add_argument("--pair",   default="both",
                        choices=["USD_JPY", "EUR_USD", "GBP_USD", "AUD_USD",
                                 "AUD_JPY", "GBP_JPY", "EUR_JPY", "EUR_GBP",
                                 "all", "cross", "both"])
    parser.add_argument("--tf",     default="H1", choices=["M5", "H1"])
    parser.add_argument("--source", default="yfinance",
                        choices=["yfinance", "av"],
                        help="データソース (yfinance=無料・制限あり / av=Alpha Vantage)")
    parser.add_argument("--months", type=int, default=24,
                        help="AVで取得する月数 (デフォルト: 24)")
    parser.add_argument("--days", type=int, default=None,
                        help="直近 N 日のみ使用（例: --days 60）")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--strategy", default="bb", choices=["bb", "trend"],
                        help="戦略 (bb=BBレンジ逆張り / trend=MAパーフェクトオーダー順張り)")
    parser.add_argument("--grid", default="default",
                        choices=["default", "adx", "rsi", "best", "sl", "sl_touch"],
                        help="グリッド種別 (default / adx / rsi / best / sl=SLスイープ / sl_touch=タッチエントリーSLスイープ)")
    parser.add_argument("--csv", action="store_true",
                        help="トレード明細を CSV で出力")
    parser.add_argument("--detail", action="store_true",
                        help="ベスト設定の詳細レポートも表示")
    args = parser.parse_args()

    if args.pair == "both":
        pairs = ["USD_JPY", "EUR_USD"]
    elif args.pair == "all":
        pairs = ["EUR_USD", "GBP_USD", "AUD_USD"]
    elif args.pair == "cross":
        pairs = ["EUR_JPY", "GBP_JPY", "AUD_JPY", "GBP_USD", "EUR_GBP"]
    else:
        pairs = [args.pair]

    # AV を使う場合は API キーが必要
    av_key = ""
    if args.source == "av":
        from src.config import ALPHA_VANTAGE_API_KEY
        av_key = ALPHA_VANTAGE_API_KEY
        if not av_key:
            print("エラー: .env に ALPHA_VANTAGE_API_KEY が設定されていません")
            print("  https://www.alphavantage.co/support/#api-key で無料取得できます")
            return

    if args.no_cache:
        for p in pairs:
            if args.source == "yfinance":
                cp = _cache_path(p, args.tf)
                if cp.exists():
                    cp.unlink()
            else:
                cp = _av_merged_cache_path(p, args.tf, args.months)
                if cp.exists():
                    cp.unlink()

    all_trades: list[dict] = []

    for pair in pairs:
        try:
            if args.source == "av":
                print(f"\n[{pair}] Alpha Vantage から {args.months} ヶ月分取得します")
                print("  無料枠: 25リクエスト/日。2年分=24リクエスト/ペア")
                print("  月ごとにキャッシュするため途中停止→再実行でも続きから取得できます")
                candles = load_or_fetch_av(pair, args.tf, args.months, av_key)
            else:
                candles = load_or_fetch(pair, args.tf)
        except Exception as e:
            print(f"  データ取得失敗: {e}")
            continue

        if args.days:
            cutoff = candles[-1].time - timedelta(days=args.days)
            candles = [c for c in candles if c.time >= cutoff]
            logger.info("直近 %d 日に絞り込み: %d 本", args.days, len(candles))

        spread = SPREAD_PIPS.get(pair, 0.7)

        if args.strategy == "trend":
            configs = default_trend_grid(spread)
            rows = run_trend_grid(candles, pair, configs)
        else:
            if args.grid == "adx":
                configs = adx_sweep_grid(spread)
            elif args.grid == "rsi":
                configs = rsi_sweep_grid(spread)
            elif args.grid == "best":
                configs = best_config(spread)
            elif args.grid == "sl":
                configs = sl_sweep_grid(spread)
            elif args.grid == "sl_touch":
                configs = sl_touch_grid(spread)
            else:
                configs = default_grid(spread)
            rows = run_grid(candles, pair, configs)

        # --detail: 最もPFが高い設定の詳細
        if args.detail and rows:
            best = max(rows, key=lambda r: r["all"]["pf"] if r["all"]["total"] > 5 else 0)
            print_report(best["trades"], f"{pair} 詳細: {best['cfg'].label}")

        for row in rows:
            all_trades.extend(row["trades"])

    RESULTS_FILE.parent.mkdir(exist_ok=True)
    RESULTS_FILE.write_text(
        json.dumps(all_trades, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n結果保存: {RESULTS_FILE}")

    if args.csv:
        label = f"_{args.pair}" if args.pair not in ("both", "all", "cross") else ""
        _export_csv(all_trades, label)


def _export_csv(trades: list[dict], suffix: str = "") -> None:
    import csv
    name = f"backtest_results{suffix}.csv"
    csv_file = RESULTS_FILE.parent / name
    fields = [
        "pair", "direction", "result",
        "entry_time", "exit_time",
        "entry", "sl", "tp", "exit",
        "pips", "session",
    ]
    with csv_file.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow({
                "pair":       t.get("pair", ""),
                "direction":  t.get("direction", ""),
                "result":     t.get("result", ""),
                "entry_time": t.get("entry_time", ""),
                "exit_time":  t.get("exit_time", ""),
                "entry":      t.get("entry", ""),
                "sl":         t.get("sl", ""),
                "tp":         t.get("tp", ""),
                "exit":       t.get("exit", ""),
                "pips":       t.get("pips", ""),
                "session":    t.get("session", ""),
            })
    print(f"CSV出力: {csv_file}  ({len(trades)} 件)")


if __name__ == "__main__":
    main()
