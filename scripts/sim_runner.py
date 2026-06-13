"""
BB レンジ逆張り ペーパートレード ランナー

戦略: BB(20,2) ±2σ タッチ + RSI(14) 35/65 + ADX(14) < 30
対象: EUR/USD, GBP/JPY  (H1)

動作:
  - 60 秒ごとにポーリング
  - H1 新足確定時にシグナル判定 → エントリー
  - TP/SL ヒット監視 → 決済
  - 1 ペアに同時 1 ポジションまで
  - 1 日 2 連敗で当日そのペアは停止

使い方:
    python scripts/sim_runner.py
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG_FILE = ROOT / "data" / "sim_runner.log"
LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

JST              = timezone(timedelta(hours=9))
POLL_SEC         = 60
STATE_FILE       = ROOT / "data" / "sim_state.json"
RESULTS_FILE     = ROOT / "data" / "sim_results.json"

# --- 戦略パラメータ ---
PAIRS            = ["EUR_USD", "GBP_JPY"]
TF               = "H1"
CANDLE_COUNT     = 120    # 指標ウォームアップ込み

ADX_THRESHOLD    = 30.0
RSI_OB           = 65.0
RSI_OS           = 35.0
SL_BUFFER_PIPS   = 2.0
RR_MIN           = 2.0

SPREAD_PIPS      = {"EUR_USD": 0.7, "GBP_JPY": 1.2}
MAX_DAILY_LOSSES = 2


# ------------------------------------------------------------------
# 状態管理
# ------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {p: {"open_trade": None, "last_bar": None, "daily_losses": {}} for p in PAIRS}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_result(trade: dict) -> None:
    results: list = []
    if RESULTS_FILE.exists():
        try:
            results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    results.append(trade)
    RESULTS_FILE.parent.mkdir(exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# Discord 通知
# ------------------------------------------------------------------

def _notify(msg: str) -> None:
    try:
        from src.config import DISCORD_WEBHOOK_URL
        from src.notifications.discord import send_discord
        if DISCORD_WEBHOOK_URL:
            send_discord(DISCORD_WEBHOOK_URL, msg)
    except Exception as e:
        logger.warning("Discord通知失敗: %s", e)


# ------------------------------------------------------------------
# エントリー処理
# ------------------------------------------------------------------

def _try_entry(pair: str, candles: list, pair_state: dict, now: datetime) -> dict:
    from src.trading.signal_engine import detect_range_reversal

    bar_time = candles[-1].time.isoformat()

    # 同じ足で二重エントリーしない
    if pair_state.get("last_bar") == bar_time:
        return pair_state

    # オープントレードがあれば新規エントリーしない
    if pair_state.get("open_trade"):
        return pair_state

    # 当日連敗上限チェック
    today = now.strftime("%Y-%m-%d")
    daily = pair_state.get("daily_losses", {})
    if daily.get(today, 0) >= MAX_DAILY_LOSSES:
        logger.info("%s: 当日連敗上限(%d)到達 → スキップ", pair, MAX_DAILY_LOSSES)
        return pair_state

    signal = detect_range_reversal(
        candles, pair,
        adx_threshold=ADX_THRESHOLD,
        rsi_ob=RSI_OB,
        rsi_os=RSI_OS,
        sl_buffer_pips=SL_BUFFER_PIPS,
        rr_min=RR_MIN,
    )

    pair_state["last_bar"] = bar_time

    if signal is None:
        return pair_state

    # スプレッドを入口に加算
    pip = 0.01 if pair.endswith("JPY") else 0.0001
    spread_cost = SPREAD_PIPS.get(pair, 0.7) * pip
    if signal.direction == "BUY":
        entry = signal.entry_price + spread_cost
    else:
        entry = signal.entry_price - spread_cost

    trade = {
        "pair":        pair,
        "direction":   signal.direction,
        "entry_price": round(entry, 5),
        "entry_time":  now.isoformat(),
        "sl_price":    signal.sl_price,
        "tp_price":    signal.tp_price,
        "rr":          signal.rr,
        "setup_time":  signal.setup_time,
        "confirm_time": signal.confirm_time,
    }
    pair_state["open_trade"] = trade

    pair_label = pair.replace("_", "/")
    logger.info("エントリー: %s %s @ %.5f  SL=%.5f TP=%.5f  RR=%.1f",
                pair_label, signal.direction, entry,
                signal.sl_price, signal.tp_price, signal.rr)
    _notify(
        f"**📌 【エントリー】{pair_label} {signal.direction}**\n"
        f"価格: {entry:.5f}\n"
        f"SL: {signal.sl_price:.5f}  /  TP: {signal.tp_price:.5f}\n"
        f"RR: 1:{signal.rr:.1f}\n"
        f"⏰ {now.strftime('%H:%M JST')}"
    )
    return pair_state


# ------------------------------------------------------------------
# 決済処理
# ------------------------------------------------------------------

def _try_exit(pair: str, candles: list, pair_state: dict, now: datetime) -> dict:
    trade = pair_state.get("open_trade")
    if not trade:
        return pair_state

    high = candles[-1].high
    low  = candles[-1].low

    result = None
    if trade["direction"] == "BUY":
        if low  <= trade["sl_price"]: result = "SL"
        elif high >= trade["tp_price"]: result = "TP"
    else:
        if high >= trade["sl_price"]: result = "SL"
        elif low  <= trade["tp_price"]: result = "TP"

    # 23:55 JST 強制決済
    if result is None and now.hour == 23 and now.minute >= 55:
        result = "EOD"

    if result is None:
        return pair_state

    pip = 0.01 if pair.endswith("JPY") else 0.0001
    if result == "TP":
        exit_price = trade["tp_price"]
    elif result == "SL":
        exit_price = trade["sl_price"]
    else:
        exit_price = candles[-1].close

    pips = (exit_price - trade["entry_price"]) / pip if trade["direction"] == "BUY" \
           else (trade["entry_price"] - exit_price) / pip

    closed = {**trade, "exit_price": exit_price, "exit_time": now.isoformat(),
              "result": result, "pips": round(pips, 1)}
    _save_result(closed)

    pair_label = pair.replace("_", "/")
    logger.info("決済: %s %s → %s  %+.1f pips @ %.5f",
                pair_label, trade["direction"], result, pips, exit_price)

    icon = {"TP": "✅", "SL": "❌", "EOD": "🔄"}.get(result, "🔄")
    _notify(
        f"**{icon} 【決済】{pair_label} {trade['direction']} → {result}**\n"
        f"エントリー: {trade['entry_price']:.5f}\n"
        f"決済: {exit_price:.5f}\n"
        f"結果: {pips:+.1f} pips\n"
        f"⏰ {now.strftime('%H:%M JST')}"
    )

    if result == "SL":
        today = now.strftime("%Y-%m-%d")
        daily = pair_state.get("daily_losses", {})
        daily[today] = daily.get(today, 0) + 1
        pair_state["daily_losses"] = daily

    pair_state["open_trade"] = None
    return pair_state


# ------------------------------------------------------------------
# メインループ
# ------------------------------------------------------------------

def run() -> None:
    from src.data.client_factory import get_data_client

    client = get_data_client()
    logger.info("=== sim_runner 起動 | 対象: %s | ADX<%.0f RSI%.0f/%.0f ===",
                ", ".join(PAIRS), ADX_THRESHOLD, RSI_OS, RSI_OB)

    while True:
        now = datetime.now(JST)

        # 土日スキップ
        if now.weekday() >= 5:
            time.sleep(POLL_SEC)
            continue

        state = _load_state()

        for pair in PAIRS:
            pair_state = state.setdefault(
                pair, {"open_trade": None, "last_bar": None, "daily_losses": {}}
            )
            try:
                candles = client.get_candles(pair, TF, CANDLE_COUNT)
                if not candles or len(candles) < 40:
                    logger.warning("%s: ローソク足不足 (%d本)", pair, len(candles) if candles else 0)
                    continue

                # 決済チェック（先に行う）
                pair_state = _try_exit(pair, candles, pair_state, now)

                # エントリーチェック（ポジションなし時のみ）
                pair_state = _try_entry(pair, candles, pair_state, now)

                state[pair] = pair_state

            except Exception as e:
                logger.error("%s: エラー %s", pair, e)

        _save_state(state)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    run()
