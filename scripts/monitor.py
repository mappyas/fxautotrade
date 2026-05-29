"""
TP/SL モニター

エントリーは plan_job.py が担当。
このスクリプトはオープントレードの TP/SL 監視・決済のみを行う。

使い方:
    python scripts/monitor.py
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
        logging.FileHandler("data/monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
TRADE_START_HOUR = 9
TRADE_END_HOUR   = 24
POLL_INTERVAL_SEC = 60

RESULTS_FILE = Path("data/sim_results.json")
STATE_FILE   = Path("data/sim_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"open_trade": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def save_result(trade: dict) -> None:
    results: list = []
    if RESULTS_FILE.exists():
        try:
            results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    results.append(trade)
    RESULTS_FILE.parent.mkdir(exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def _notify(msg: str) -> None:
    try:
        from src.config import DISCORD_WEBHOOK_URL
        from src.notifications.discord import send_discord
        if DISCORD_WEBHOOK_URL:
            send_discord(DISCORD_WEBHOOK_URL, msg)
    except Exception as e:
        logger.warning("Discord通知失敗: %s", e)


def _check_exit(trade: dict, high: float, low: float) -> str | None:
    direction = trade["direction"]
    sl = trade["sl_price"]
    tp = trade["tp_price"]
    if direction == "BUY":
        if low  <= sl: return "SL"
        if high >= tp: return "TP"
    else:
        if high >= sl: return "SL"
        if low  <= tp: return "TP"
    return None


def _close_trade(trade: dict, result: str, current: float, now: datetime) -> dict:
    if result == "TP":
        exit_price = trade["tp_price"]
    elif result == "SL":
        exit_price = trade["sl_price"]
    else:
        exit_price = current

    pip = 0.01 if trade["pair"].endswith("JPY") else 0.0001
    direction = trade["direction"]
    pips = round((exit_price - trade["entry_price"]) / pip) if direction == "BUY" \
           else round((trade["entry_price"] - exit_price) / pip)

    closed = {**trade, "exit_price": exit_price, "exit_time": now.isoformat(),
              "result": result, "pips": pips}
    save_result(closed)

    pair_label = trade["pair"].replace("_", "/")
    logger.info("決済: %s %s → %s %+dpips @ %.5f", pair_label, direction, result, pips, exit_price)

    result_icon = {"TP": "✅", "SL": "❌"}.get(result, "🔄")
    _notify(
        f"**{result_icon} 【決済】{pair_label} {direction} → {result}**\n"
        f"エントリー: {trade['entry_price']:.5f}\n"
        f"決済: {exit_price:.5f}\n"
        f"結果: {pips:+d} pips\n"
        f"⏰ {now.strftime('%H:%M JST')}"
    )
    return closed


def _run_check(trade: dict) -> None:
    try:
        from src.ai.checker import run_check
        result = run_check(trade)
        if result:
            logger.info("Check完了: bias_correct=%s | %s", result.bias_correct, result.cause)
            bias_icon = "🎯" if result.bias_correct else "❗"
            _notify(
                f"**🔍 【Action】{result.pair.replace('_', '/')}**\n"
                f"Plan的中: {bias_icon}\n"
                f"原因: {result.cause}\n"
                f"改善: {result.improvement}"
            )
    except Exception as e:
        logger.error("Check実行失敗: %s", e)


def run() -> None:
    from src.data.client_factory import get_data_client

    Path("data").mkdir(exist_ok=True)
    client = get_data_client()
    logger.info("=== モニター開始（TP/SL監視）===")

    while True:
        now = datetime.now(JST)

        if now.weekday() >= 5 or not (TRADE_START_HOUR <= now.hour < TRADE_END_HOUR):
            time.sleep(POLL_INTERVAL_SEC)
            continue

        state = load_state()
        open_trade = state.get("open_trade")

        if not open_trade:
            time.sleep(POLL_INTERVAL_SEC)
            continue

        pair = open_trade["pair"]
        try:
            candles = client.get_candles(pair, "H1", 5)
            if not candles:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            high    = candles[-1].high
            low     = candles[-1].low
            current = candles[-1].close

            result = _check_exit(open_trade, high, low)
            if result:
                closed = _close_trade(open_trade, result, current, now)
                save_state({"open_trade": None})
                _run_check(closed)

        except Exception as e:
            logger.error("モニターエラー %s: %s", pair, e)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    run()
