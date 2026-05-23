"""
Checkフェーズジョブ

実行タイミング（JST）:
    23:30  FINAL直後に実行（未チェックのトレードを全件評価）

使い方:
    python scripts/check_job.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG_FILE = ROOT / "data" / "check_job.log"
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


def _format_discord(results: list, now: datetime) -> str:
    lines = [
        "**【Checkフェーズ】トレード評価レポート**",
        f"⏰ {now.strftime('%H:%M JST')}",
        f"評価件数: {len(results)} 件",
        "",
    ]

    tp_count  = sum(1 for r in results if r.trade.get("result") == "TP")
    sl_count  = sum(1 for r in results if r.trade.get("result") == "SL")
    total_pips = sum(r.trade.get("pips", 0) for r in results)
    bias_ok   = sum(1 for r in results if r.bias_correct)

    lines += [
        f"TP: {tp_count} / SL: {sl_count}　合計: {total_pips:+d} pips",
        f"Plan bias的中率: {bias_ok}/{len(results)}",
        "",
    ]

    for r in results:
        trade  = r.trade
        result = trade.get("result", "?")
        pips   = trade.get("pips", 0)
        icon   = "✅" if result == "TP" else "❌"
        bias_icon = "🎯" if r.bias_correct else "❗"

        lines += [
            f"{icon} **{r.pair.replace('_', '/')} {trade.get('direction')} → {result} {pips:+d}pips** {bias_icon}",
            f"　原因: {r.cause}",
            f"　改善: {r.improvement}",
        ]

    return "\n".join(lines)


def main() -> None:
    from src.ai.checker import run_check_all
    from src.config import DISCORD_WEBHOOK_URL
    from src.notifications.discord import send_discord

    now = datetime.now(JST)
    logger.info("=== Checkフェーズ開始 | %s ===", now.strftime("%H:%M JST"))

    results = run_check_all()

    if not results:
        logger.info("未チェックのトレードなし")
        return

    logger.info("評価完了: %d 件", len(results))

    if DISCORD_WEBHOOK_URL:
        msg = _format_discord(results, now)
        send_discord(DISCORD_WEBHOOK_URL, msg)
        logger.info("Discord通知送信")

    logger.info("=== Checkフェーズ完了 ===")


if __name__ == "__main__":
    main()
