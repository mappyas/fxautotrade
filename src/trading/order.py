"""注文実行モジュール"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.ai.analyzer import Signal
from src.config import (
    DEFAULT_SL_PIPS,
    DEFAULT_TP_PIPS,
    MAX_DAILY_LOSS,
    MAX_POSITIONS,
    PAPER_TRADE,
    RISK_PCT,
    USE_AI_SLTP,
)
from src.data.base_client import BaseDataClient
from src.data.oanda_client import OrderResult
from src.trading.risk import calc_position_size
from src.trading.signal import ValidationResult, validate_signal

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    executed: bool
    order: OrderResult | None = None
    reason: str = ""
    paper: bool = False


def execute(
    client: BaseDataClient,
    signal: Signal,
    pair: str,
    daily_pnl: float = 0.0,
    economic_events: list[dict] | None = None,
) -> ExecutionResult:
    """
    シグナルを受け取り、バリデーション後に注文を実行する。
    PAPER_TRADE=True の場合はログのみで実際には注文しない。
    """
    account    = client.get_account_summary()
    positions  = client.get_open_positions()

    # バリデーション
    validation = validate_signal(
        signal, positions, daily_pnl,
        MAX_DAILY_LOSS, MAX_POSITIONS, pair, economic_events,
    )
    if not validation.ok:
        logger.info("[SKIP] %s | %s", pair, validation.reason)
        return ExecutionResult(executed=False, reason=validation.reason)

    # SL / TP 価格を計算
    sl_pips = signal.suggested_sl_pips if USE_AI_SLTP else DEFAULT_SL_PIPS
    tp_pips = signal.suggested_tp_pips if USE_AI_SLTP else DEFAULT_TP_PIPS
    current_price = client.get_candles(pair, "H1", 1)[-1].close

    sl_price, tp_price = _calc_sl_tp(signal.action, current_price, sl_pips, tp_pips, pair)

    # USDJPYレート取得（非JPYペアのポジションサイズ換算用）
    usdjpy_rate = _get_usdjpy_rate(client, pair, current_price)

    # ポジションサイズ計算
    units = calc_position_size(account.balance, RISK_PCT, sl_pips, pair, usdjpy_rate)
    if signal.action == "SELL":
        units = -units

    if PAPER_TRADE:
        logger.info(
            "[PAPER] %s %s %d units @ %.5f | SL=%.5f TP=%.5f | %s",
            signal.action, pair, units, current_price, sl_price, tp_price,
            signal.reasoning,
        )
        return ExecutionResult(executed=True, reason="ペーパートレード", paper=True)

    # 実注文
    order = client.create_market_order(pair, units, sl_price, tp_price)
    logger.info(
        "[ORDER] %s %s %d units | trade_id=%s | SL=%.5f TP=%.5f",
        signal.action, pair, units, order.trade_id, sl_price, tp_price,
    )
    return ExecutionResult(executed=True, order=order)


# ------------------------------------------------------------------
# SL / TP 価格計算
# ------------------------------------------------------------------

def _get_usdjpy_rate(client, pair: str, current_price: float) -> float:
    """USD/JPYレートを取得する。JPYクロスの場合は不要なので150.0を返す"""
    if pair.endswith("JPY"):
        return 150.0  # JPYクロスは使わないので任意の値
    try:
        candles = client.get_candles("USD_JPY", "H1", 1)
        return candles[-1].close if candles else 150.0
    except Exception:
        return 150.0  # 取得失敗時はデフォルト値


def _calc_sl_tp(
    action: str,
    price: float,
    sl_pips: int,
    tp_pips: int,
    pair: str,
) -> tuple[float, float]:
    """pips から SL/TP の実際の価格を計算する"""
    pip = 0.01 if pair.endswith("JPY") else 0.0001
    sl_delta = sl_pips * pip
    tp_delta = tp_pips * pip

    if action == "BUY":
        return round(price - sl_delta, 5), round(price + tp_delta, 5)
    else:  # SELL
        return round(price + sl_delta, 5), round(price - tp_delta, 5)
