"""
FX AutoBuy メインエントリーポイント

Cloud Scheduler から15分ごとに呼び出される。
全ペアに対してデータ取得 → AI推論 → リスク管理 → 注文実行 を行う。
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone

from src.config import CANDLE_COUNTS, PAIRS, PAPER_TRADE
from src.ai.analyzer import analyze
from src.data.client_factory import get_data_client
from src.trading.order import execute

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def run(daily_pnl: float = 0.0) -> None:
    logger.info("=" * 60)
    logger.info("FX AutoBuy 起動 | %s | PAPER=%s", datetime.now(timezone.utc).isoformat(), PAPER_TRADE)
    logger.info("=" * 60)

    client = get_data_client()
    account = client.get_account_summary()
    logger.info("口座残高: %.0f %s | NAV: %.0f", account.balance, account.currency, account.nav)

    for i, pair in enumerate(PAIRS):
        if i > 0:
            time.sleep(5)  # Gemini RPM制限対策
        logger.info("-" * 40)
        logger.info("分析中: %s", pair)

        try:
            _process_pair(client, pair, daily_pnl)
        except Exception as e:
            logger.error("[ERROR] %s の処理中にエラー: %s", pair, e, exc_info=True)

    logger.info("=" * 60)
    logger.info("処理完了")


def _process_pair(client, pair: str, daily_pnl: float) -> None:
    # 1. データ取得
    candles = client.get_multi_granularity_candles(pair, CANDLE_COUNTS)
    positions = client.get_open_positions()

    if not candles.get("H1"):
        logger.warning("%s: ローソク足データが空のためスキップ", pair)
        return

    current_price = candles["H1"][-1].close
    logger.info("現在値: %.5f | H1本数: %d", current_price, len(candles["H1"]))

    # 2. AI推論
    signal = analyze(
        pair=pair,
        candles_h1=candles.get("H1", []),
        candles_h4=candles.get("H4", []),
        candles_d=candles.get("D", []),
        open_positions=positions,
    )
    logger.info(
        "シグナル: %s | confidence=%.2f | model=%s | fallback=%s",
        signal.action, signal.confidence, signal.model_used, signal.fallback_used,
    )
    logger.info("判断理由: %s", signal.reasoning)

    # 3. 注文実行
    result = execute(client, signal, pair, daily_pnl)
    if result.executed:
        status = "PAPER" if result.paper else "EXECUTED"
        logger.info("[%s] %s 完了", status, pair)
    else:
        logger.info("[SKIP] %s", result.reason)


if __name__ == "__main__":
    run()
