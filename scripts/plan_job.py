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


def _format_discord(plan, now: datetime) -> str:
    from src.ai.planner import SESSIONS
    session_label = SESSIONS.get(plan.session, plan.session)
    bias_icon = {"BUY": "📈", "SELL": "📉", "NEUTRAL": "⏸️"}.get(plan.bias, "⏸️")
    pair_label = plan.pair.replace("_", "/")

    lines = [
        f"**【Planフェーズ】{session_label}**",
        f"⏰ {now.strftime('%H:%M JST')}",
        "",
        f"{bias_icon} **{pair_label}　{plan.bias}**",
    ]

    if plan.avoid_until:
        avoid_dt = datetime.fromisoformat(plan.avoid_until)
        lines.append(f"⚠️ エントリー禁止〜{avoid_dt.strftime('%H:%M JST')}")

    lines += [
        "",
        f"**ファンダ:** {plan.fundamental}",
        "",
        f"**テクニカル:** {plan.technical}",
        "",
    ]

    for p in plan.plans:
        label   = p.get("label", "?")
        cond    = p.get("condition", "")
        entry   = p.get("entry", "HOLD")
        sl      = p.get("sl_pips")
        tp      = p.get("tp_pips")
        notes   = p.get("notes", "")
        entry_icon = {"BUY": "📈", "SELL": "📉", "HOLD": "⏸️"}.get(entry, "⏸️")

        sl_tp = f"SL:{sl}pips / TP:{tp}pips" if sl and tp else "—"
        lines.append(f"**Plan {label}** {entry_icon} {entry}　{sl_tp}")
        lines.append(f"　条件: {cond}")
        if notes:
            lines.append(f"　{notes}")

    return "\n".join(lines)


def main() -> None:
    from src.ai.indicators import calc_indicators
    from src.ai.planner import SESSIONS, run_plan
    from src.config import DISCORD_WEBHOOK_URL, FINNHUB_API_KEY, PAIRS
    from src.data.client_factory import get_data_client
    from src.data.economic_calendar import fetch_economic_events
    from src.notifications.discord import send_discord

    now = datetime.now(JST)
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

            logger.info("%s: bias=%s plans=%d", pair, plan.bias, len(plan.plans))

            if DISCORD_WEBHOOK_URL:
                msg = _format_discord(plan, now)
                send_discord(DISCORD_WEBHOOK_URL, msg)

        except Exception as e:
            logger.error("%s: エラー %s", pair, e)

    logger.info("=== Planフェーズ完了 ===")


if __name__ == "__main__":
    main()
