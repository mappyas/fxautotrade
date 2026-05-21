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


def main() -> None:
    from src.ai.indicators import calc_indicators
    from src.ai.planner import SESSIONS, run_plan
    from src.config import DISCORD_WEBHOOK_URL, PAIRS
    from src.data.client_factory import get_data_client
    from src.data.economic_calendar import fetch_economic_events
    from src.notifications.discord import send_discord

    now = datetime.now(JST)
    session = _detect_session(now)
    session_label = SESSIONS.get(session, session)

    logger.info("=== Planフェーズ開始 | %s | %s ===", session, now.strftime("%H:%M JST"))

    client = get_data_client()

    try:
        from src.config import FINNHUB_API_KEY
        events = fetch_economic_events(FINNHUB_API_KEY) if FINNHUB_API_KEY else []
    except Exception as e:
        logger.warning("経済指標取得失敗: %s", e)
        events = []

    results = []
    for pair in PAIRS:
        try:
            candles_h1 = client.get_candles(pair, "H1", 100)
            candles_h4 = client.get_candles(pair, "H4", 30)
            candles_d  = client.get_candles(pair, "D",  20)

            if not candles_h1:
                logger.warning("%s: ローソク足データなし", pair)
                continue

            ind = calc_indicators(candles_h1)
            plan = run_plan(pair, session, candles_h1, candles_h4, candles_d, ind, events)
            results.append(plan)

            logger.info(
                "%s: bias=%s avoid_until=%s | %s",
                pair, plan.bias, plan.avoid_until, plan.notes,
            )
        except Exception as e:
            logger.error("%s: エラー %s", pair, e)

    if not results:
        logger.warning("Planを生成できたペアがありません")
        return

    # Discord通知
    if DISCORD_WEBHOOK_URL:
        bias_icon = {"BUY": "📈", "SELL": "📉", "NEUTRAL": "⏸️"}
        lines = [
            f"**【Planフェーズ】{session_label}**",
            f"⏰ {now.strftime('%H:%M JST')}",
            "",
        ]
        for plan in results:
            icon = bias_icon.get(plan.bias, "⏸️")
            lines.append(f"{icon} **{plan.pair.replace('_', '/')}**: {plan.bias}")
            if plan.avoid_until:
                avoid_dt = datetime.fromisoformat(plan.avoid_until)
                lines.append(f"　⚠️ エントリー禁止〜{avoid_dt.strftime('%H:%M JST')}")
            lines.append(f"　{plan.notes}")

        send_discord(DISCORD_WEBHOOK_URL, "\n".join(lines))

    logger.info("=== Planフェーズ完了 ===")


if __name__ == "__main__":
    main()
