"""
FX 自動売買シミュレーター（ローカル実行専用・AIなし）

使い方:
    python scripts/sim_runner.py

動作:
    - 1分ごとにポーリング
    - テクニカル条件でエントリー（仮想）
    - TP/SL到達で決済 → Discord通知
    - 結果を data/sim_results.json に保存
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/sim_runner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
JST = timezone(timedelta(hours=9))

START_TIME = datetime(2026, 4, 23, 23, 0, tzinfo=JST)
END_TIME   = datetime(2026, 4, 24, 19, 0, tzinfo=JST)

POLL_INTERVAL_SEC = 60   # 1分ごと
SL_PIPS = 50
TP_PIPS = 100

PAIRS = ["USD_JPY", "EUR_USD"]

RESULTS_FILE = Path("data/sim_results.json")
STATE_FILE   = Path("data/sim_state.json")

# 条件 → エントリー方向
CONDITION_DIRECTION = {
    "MACD_BULL":      "BUY",
    "RSI_OVERSOLD":   "BUY",
    "MACD_BEAR":      "SELL",
    "RSI_OVERBOUGHT": "SELL",
}


# ------------------------------------------------------------------
# 状態管理
# ------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"open_trade": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_results() -> list:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    return []


def save_result(trade: dict) -> None:
    results = load_results()
    results.append(trade)
    RESULTS_FILE.parent.mkdir(exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# pip 計算
# ------------------------------------------------------------------

def pip_value(pair: str) -> float:
    return 0.01 if pair.endswith("JPY") else 0.0001


def calc_sl_tp(pair: str, direction: str, entry: float) -> tuple[float, float]:
    pip = pip_value(pair)
    if direction == "BUY":
        return entry - SL_PIPS * pip, entry + TP_PIPS * pip
    else:
        return entry + SL_PIPS * pip, entry - TP_PIPS * pip


# ------------------------------------------------------------------
# Discord 通知
# ------------------------------------------------------------------

def notify(msg: str) -> None:
    from src.config import DISCORD_WEBHOOK_URL
    from src.notifications.discord import send_discord
    if DISCORD_WEBHOOK_URL:
        send_discord(DISCORD_WEBHOOK_URL, msg)
    logger.info("Discord: %s", msg)


# ------------------------------------------------------------------
# メインループ
# ------------------------------------------------------------------

def run() -> None:
    from src.ai.indicators import calc_indicators
    from src.data.client_factory import get_data_client
    from src.notifications.alert_filter import _detect_condition

    Path("data").mkdir(exist_ok=True)
    client = get_data_client()

    now = datetime.now(JST)

    # 開始時刻まで待機
    if now < START_TIME:
        wait_sec = (START_TIME - now).total_seconds()
        logger.info("開始まで %.0f 秒待機 (%s JST)", wait_sec, START_TIME.strftime("%H:%M"))
        time.sleep(wait_sec)

    logger.info("=== シミュレーター開始 ===")
    logger.info("期間: %s 〜 %s", START_TIME.strftime("%m/%d %H:%M"), END_TIME.strftime("%m/%d %H:%M"))
    logger.info("SL=%dpips / TP=%dpips / 対象ペア=%s", SL_PIPS, TP_PIPS, PAIRS)
    notify(f"**【シミュレーター開始】**\n期間: {START_TIME.strftime('%m/%d %H:%M')} 〜 {END_TIME.strftime('%m/%d %H:%M')} JST\nSL={SL_PIPS}pips / TP={TP_PIPS}pips")

    while True:
        now = datetime.now(JST)

        # 終了判定
        if now >= END_TIME:
            state = load_state()
            if state["open_trade"]:
                _force_close(state, client)
            logger.info("=== シミュレーター終了 ===")
            _print_summary()
            notify("**【シミュレーター終了】** 結果を確認してください。")
            break

        state = load_state()
        open_trade = state.get("open_trade")

        for pair in PAIRS:
            try:
                candles = client.get_candles(pair, "H1", 100)
                if not candles:
                    continue

                ind = calc_indicators(candles)
                current  = candles[-1].close
                high     = candles[-1].high
                low      = candles[-1].low

                if open_trade and open_trade["pair"] == pair:
                    # TP/SL チェック
                    result = _check_exit(open_trade, high, low, current, now)
                    if result:
                        _close_trade(state, open_trade, result, current, now)
                        save_state({"open_trade": None})

                elif open_trade is None:
                    # エントリー判定
                    condition = _detect_condition(ind)
                    if condition:
                        key, _ = condition
                        direction = CONDITION_DIRECTION.get(key)
                        if direction:
                            sl, tp = calc_sl_tp(pair, direction, current)
                            trade = {
                                "pair":        pair,
                                "direction":   direction,
                                "entry_price": current,
                                "entry_time":  now.isoformat(),
                                "sl_price":    round(sl, 5),
                                "tp_price":    round(tp, 5),
                                "condition":   key,
                            }
                            state["open_trade"] = trade
                            save_state(state)

                            pair_label = pair.replace("_", "/")
                            logger.info("エントリー: %s %s @ %.5f (SL=%.5f / TP=%.5f)", pair_label, direction, current, sl, tp)
                            notify(
                                f"**【エントリー】{pair_label} {direction}**\n"
                                f"価格: {current:.5f}\n"
                                f"SL: {sl:.5f} / TP: {tp:.5f}\n"
                                f"条件: {key}\n"
                                f"⏰ {now.strftime('%H:%M JST')}"
                            )

            except Exception as e:
                logger.error("%s: エラー %s", pair, e)

        time.sleep(POLL_INTERVAL_SEC)


def _check_exit(trade: dict, high: float, low: float, current: float, now: datetime) -> str | None:
    direction = trade["direction"]
    sl = trade["sl_price"]
    tp = trade["tp_price"]

    if direction == "BUY":
        if low <= sl:
            return "SL"
        if high >= tp:
            return "TP"
    else:
        if high >= sl:
            return "SL"
        if low <= tp:
            return "TP"
    return None


def _close_trade(state: dict, trade: dict, result: str, current: float, now: datetime) -> None:
    exit_price = trade["tp_price"] if result == "TP" else trade["sl_price"]
    pip = pip_value(trade["pair"])
    direction = trade["direction"]

    if direction == "BUY":
        pips = round((exit_price - trade["entry_price"]) / pip)
    else:
        pips = round((trade["entry_price"] - exit_price) / pip)

    closed = {**trade, "exit_price": exit_price, "exit_time": now.isoformat(), "result": result, "pips": pips}
    save_result(closed)

    pair_label = trade["pair"].replace("_", "/")
    icon = "✅" if result == "TP" else "❌"
    logger.info("決済: %s %s → %s %+dpips", pair_label, direction, result, pips)
    notify(
        f"**{icon} 【決済】{pair_label} {direction} → {result}**\n"
        f"エントリー: {trade['entry_price']:.5f}\n"
        f"決済: {exit_price:.5f}\n"
        f"結果: {pips:+d} pips\n"
        f"⏰ {now.strftime('%H:%M JST')}"
    )


def _force_close(state: dict, client) -> None:
    trade = state["open_trade"]
    if not trade:
        return
    from src.data.client_factory import get_data_client
    candles = get_data_client().get_candles(trade["pair"], "H1", 1)
    current = candles[-1].close if candles else trade["entry_price"]
    _close_trade(state, trade, "TIME", current, datetime.now(JST))
    state["open_trade"] = None
    save_state(state)


def _print_summary() -> None:
    results = load_results()
    if not results:
        logger.info("取引なし")
        return

    tp_count = sum(1 for r in results if r["result"] == "TP")
    sl_count = sum(1 for r in results if r["result"] == "SL")
    total_pips = sum(r["pips"] for r in results)

    logger.info("=== 結果サマリー ===")
    logger.info("総取引数: %d", len(results))
    logger.info("TP: %d / SL: %d", tp_count, sl_count)
    logger.info("合計pips: %+d", total_pips)

    summary = (
        f"**【シミュレーター結果】**\n"
        f"総取引数: {len(results)}\n"
        f"TP: {tp_count} / SL: {sl_count}\n"
        f"合計: {total_pips:+d} pips"
    )
    notify(summary)


if __name__ == "__main__":
    run()
