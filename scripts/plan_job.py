"""
Planフェーズジョブ

実行タイミング（JST）:
    08:30  東京セッション前
    15:30  ロンドン開始前
    20:30  NY開始前
    23:00  最終確認

使い方:
    python scripts/plan_job.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG_FILE = ROOT / "data" / "plan_job.log"
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

JST = timezone(timedelta(hours=9))

STATE_FILE   = ROOT / "data" / "sim_state.json"
TRADE_START_HOUR = 9
TRADE_END_HOUR   = 24


def _load_sim_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"open_trade": None}


def _save_sim_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _notify(msg: str) -> None:
    try:
        from src.config import DISCORD_WEBHOOK_URL
        from src.notifications.discord import send_discord
        if DISCORD_WEBHOOK_URL:
            send_discord(DISCORD_WEBHOOK_URL, msg)
    except Exception as e:
        logger.warning("Discord通知失敗: %s", e)


def _detect_session(now: datetime) -> str:
    hour = now.hour
    if hour < 15:
        return "TOKYO"
    elif hour < 20:
        return "LONDON_OPEN"
    elif hour < 23:
        return "NY_OPEN"
    else:
        return "FINAL"


def _format_discord(plan, now: datetime, entry_executed: bool = False) -> str:
    from src.ai.planner import SESSIONS
    session_label = SESSIONS.get(plan.session, plan.session)
    bias_icon = {"BUY": "📈", "SELL": "📉", "NEUTRAL": "⏸️"}.get(plan.bias, "⏸️")
    pair_label = plan.pair.replace("_", "/")

    lines = [
        f"**【Planフェーズ】{session_label}**",
        f"⏰ {now.strftime('%H:%M JST')}",
        "",
        f"{bias_icon} **{pair_label}　{plan.bias}**　SL:{plan.sl_pips}pips / TP:{plan.tp_pips}pips",
    ]

    if plan.avoid_until:
        avoid_dt = datetime.fromisoformat(plan.avoid_until)
        lines.append(f"⚠️ エントリー禁止〜{avoid_dt.strftime('%H:%M JST')}")

    lines += [
        "",
        f"**ファンダ:** {plan.fundamental}",
        "",
        f"**テクニカル:** {plan.technical}",
    ]

    if plan.entry_note:
        lines += ["", f"**方針:** {plan.entry_note}"]

    if entry_executed:
        lines += ["", "✅ このセッションでエントリー実行済み"]
    elif plan.bias == "NEUTRAL":
        lines += ["", "⏸️ エントリーなし（NEUTRAL）"]

    return "\n".join(lines)


def _execute_entry(plan, current_price: float, pair: str, now: datetime) -> bool:
    """Planのbiasに従い即エントリー。成功したらTrueを返す"""
    if plan.bias == "NEUTRAL":
        return False

    if not (TRADE_START_HOUR <= now.hour < TRADE_END_HOUR):
        logger.info("%s: 取引時間外のためエントリースキップ (%d時)", pair, now.hour)
        return False

    if plan.avoid_until:
        try:
            avoid_until = datetime.fromisoformat(plan.avoid_until)
            if now < avoid_until:
                logger.info("%s: avoid_until中のためエントリースキップ", pair)
                return False
        except Exception:
            pass

    state = _load_sim_state()
    if state.get("open_trade"):
        logger.info("%s: オープントレードあり、エントリースキップ", pair)
        return False

    direction = plan.bias
    pip = 0.01 if pair.endswith("JPY") else 0.0001
    if direction == "BUY":
        sl = current_price - plan.sl_pips * pip
        tp = current_price + plan.tp_pips * pip
    else:
        sl = current_price + plan.sl_pips * pip
        tp = current_price - plan.tp_pips * pip

    trade = {
        "pair":        pair,
        "direction":   direction,
        "entry_price": current_price,
        "entry_time":  now.isoformat(),
        "sl_price":    round(sl, 5),
        "tp_price":    round(tp, 5),
        "condition":   "PLAN",
        "plan_bias":   plan.bias,
    }
    _save_sim_state({"open_trade": trade})

    pair_label = pair.replace("_", "/")
    logger.info("エントリー実行: %s %s @ %.5f (SL=%.5f / TP=%.5f)",
                pair_label, direction, current_price, sl, tp)
    _notify(
        f"**【エントリー】{pair_label} {direction}**\n"
        f"価格: {current_price:.5f}\n"
        f"SL: {sl:.5f} / TP: {tp:.5f}\n"
        f"根拠: {plan.entry_note}\n"
        f"⏰ {now.strftime('%H:%M JST')}"
    )
    return True


def main() -> None:
    from src.ai.indicators import calc_indicators
    from src.ai.planner import SESSIONS, run_plan
    from src.config import DISCORD_WEBHOOK_URL, FINNHUB_API_KEY, PAIRS
    from src.data.client_factory import get_data_client
    from src.data.economic_calendar import fetch_economic_events
    from src.notifications.discord import send_discord

    now = datetime.now(JST)

    if now.weekday() >= 5:
        logger.info("土日のためPlanフェーズをスキップ")
        return

    session = _detect_session(now)
    session_label = SESSIONS.get(session, session)

    logger.info("=== Planフェーズ開始 | %s | %s ===", session, now.strftime("%H:%M JST"))

    client = get_data_client()

    try:
        events = fetch_economic_events(FINNHUB_API_KEY) if FINNHUB_API_KEY else []
    except Exception as e:
        logger.warning("経済指標取得失敗: %s", e)
        events = []

    for pair in PAIRS:
        try:
            candles_m5 = client.get_candles(pair, "M5",  60)
            candles_h1 = client.get_candles(pair, "H1", 100)
            candles_h4 = client.get_candles(pair, "H4",  30)
            candles_d  = client.get_candles(pair, "D",   20)

            if not candles_h1:
                logger.warning("%s: ローソク足データなし", pair)
                continue

            ind  = calc_indicators(candles_h1)
            plan = run_plan(pair, session, candles_m5, candles_h1, candles_h4, candles_d, ind, events)

            current_price = candles_h1[-1].close
            entry_executed = _execute_entry(plan, current_price, pair, now)

            logger.info("%s: bias=%s sl=%dpips tp=%dpips entry=%s",
                        pair, plan.bias, plan.sl_pips, plan.tp_pips, entry_executed)

            if DISCORD_WEBHOOK_URL:
                msg = _format_discord(plan, now, entry_executed)
                send_discord(DISCORD_WEBHOOK_URL, msg)

        except Exception as e:
            logger.error("%s: エラー %s", pair, e)

    logger.info("=== Planフェーズ完了 ===")


if __name__ == "__main__":
    main()
