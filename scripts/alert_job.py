"""
バックグラウンドアラートジョブ
GitHub Actions から15分ごとに実行される。
ブラウザ不要・AI呼び出しなし・Discord通知のみ。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    from src.ai.indicators import calc_indicators
    from src.config import DISCORD_WEBHOOK_URL, PAIRS
    from src.data.client_factory import get_data_client
    from src.notifications.alert_filter import check_and_notify

    if not DISCORD_WEBHOOK_URL:
        logger.error("DISCORD_WEBHOOK_URL が未設定")
        sys.exit(1)

    client = get_data_client()

    for pair in PAIRS:
        try:
            candles = client.get_candles(pair, "H1", 100)
            if not candles:
                logger.warning("%s: ローソク足なし", pair)
                continue

            ind = calc_indicators(candles)
            fired = check_and_notify(pair, ind, DISCORD_WEBHOOK_URL)
            if fired:
                logger.info("%s: 通知送信 (%s)", pair, fired)
            else:
                logger.info("%s: 条件なし or クールダウン中", pair)
        except Exception as e:
            logger.error("%s: エラー %s", pair, e)


if __name__ == "__main__":
    main()
