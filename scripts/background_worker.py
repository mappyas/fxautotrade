"""
FX バックグラウンドワーカー（GCP Compute Engine 常時起動用）
1分ごとにテクニカル条件をチェックしてDiscord通知する。
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
POLL_INTERVAL_SEC = 60


def main() -> None:
    from src.ai.indicators import calc_indicators
    from src.config import DISCORD_WEBHOOK_URL, PAIRS
    from src.data.client_factory import get_data_client
    from src.notifications.alert_filter import check_and_notify

    if not DISCORD_WEBHOOK_URL:
        logger.error("DISCORD_WEBHOOK_URL が未設定")
        sys.exit(1)

    logger.info("=== バックグラウンドワーカー起動 ===")
    logger.info("対象ペア: %s", PAIRS)
    logger.info("ポーリング間隔: %d秒", POLL_INTERVAL_SEC)

    client = get_data_client()

    while True:
        try:
            now = datetime.now(JST)
            for pair in PAIRS:
                try:
                    candles = client.get_candles(pair, "H1", 100)
                    if not candles:
                        continue
                    ind = calc_indicators(candles)
                    fired = check_and_notify(pair, ind, DISCORD_WEBHOOK_URL)
                    if fired:
                        logger.info("%s: 通知送信 (%s)", pair, fired)
                    else:
                        logger.debug("%s: RSI=%.1f trend=%s 条件なし/クールダウン",
                                     pair, ind.rsi14 or 0, ind.trend)
                except Exception as e:
                    logger.error("%s: エラー %s", pair, e)

        except Exception as e:
            logger.error("ループエラー: %s", e)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
