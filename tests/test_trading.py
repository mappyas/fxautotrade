"""Phase 3: リスク管理・シグナル検証・注文実行のテスト"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.ai.analyzer import Signal
from src.data.oanda_client import AccountSummary, Candle, OrderResult, Position
from src.trading.risk import (
    calc_position_size,
    check_daily_loss_limit,
    check_max_positions,
    validate_all,
)
from src.trading.signal import validate_signal, _near_high_impact_event
from src.trading.order import _calc_sl_tp, execute


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _pos(pair="USD_JPY", direction="LONG") -> Position:
    return Position(
        trade_id="t1", instrument=pair, direction=direction,
        units=10000, open_price=149.0, current_price=149.5,
        unrealized_pnl=500.0, open_time=datetime.now(timezone.utc),
    )


def _signal(action="BUY", confidence=0.80) -> Signal:
    return Signal(
        action=action, confidence=confidence, timeframe="DAY_TRADE",
        suggested_sl_pips=30, suggested_tp_pips=60, reasoning="test",
    )


def _mock_client(balance=1_000_000.0, positions=None, candle_close=149.5):
    client = MagicMock()
    client.get_account_summary.return_value = AccountSummary(
        balance=balance, unrealized_pnl=0, nav=balance,
        margin_used=0, margin_available=balance, currency="JPY",
    )
    client.get_open_positions.return_value = positions or []
    client.get_candles.return_value = [
        Candle(datetime.now(timezone.utc), 149.0, 150.0, 148.5, candle_close, 1000)
    ]
    client.create_market_order.return_value = OrderResult(
        order_id="o1", trade_id="t1", instrument="USD_JPY",
        units=10000, price=candle_close, sl=148.5, tp=151.0,
        time=datetime.now(timezone.utc),
    )
    return client


# ---------------------------------------------------------------------------
# calc_position_size
# ---------------------------------------------------------------------------

def test_calc_position_size_usdjpy():
    # 残高100万円, リスク2%, SL30pips, USD_JPY
    # リスク金額 = 20,000円
    # pip値 = 0.01円/unit
    # units = 20,000 / (30 * 0.01) = 66,666 → 切り捨て → 66,000
    units = calc_position_size(1_000_000, 2.0, 30, "USD_JPY")
    assert units == 66000


def test_calc_position_size_minimum():
    units = calc_position_size(10_000, 1.0, 100, "USD_JPY")
    assert units >= 1000


def test_calc_position_size_zero_sl():
    assert calc_position_size(1_000_000, 2.0, 0, "USD_JPY") == 0


# ---------------------------------------------------------------------------
# check_daily_loss_limit
# ---------------------------------------------------------------------------

def test_daily_loss_limit_ok():
    result = check_daily_loss_limit(-5000, 10000)
    assert result.ok is True


def test_daily_loss_limit_exceeded():
    result = check_daily_loss_limit(-10000, 10000)
    assert result.ok is False


def test_daily_loss_limit_just_under():
    result = check_daily_loss_limit(-9999, 10000)
    assert result.ok is True


# ---------------------------------------------------------------------------
# check_max_positions
# ---------------------------------------------------------------------------

def test_max_positions_ok():
    result = check_max_positions([_pos()], max_count=3)
    assert result.ok is True


def test_max_positions_exceeded():
    result = check_max_positions([_pos(), _pos(), _pos()], max_count=3)
    assert result.ok is False


def test_max_positions_same_pair():
    result = check_max_positions([_pos("USD_JPY")], max_count=3, pair="USD_JPY")
    assert result.ok is False
    assert "USD_JPY" in result.reason


def test_max_positions_different_pair():
    result = check_max_positions([_pos("EUR_USD")], max_count=3, pair="USD_JPY")
    assert result.ok is True


# ---------------------------------------------------------------------------
# _near_high_impact_event
# ---------------------------------------------------------------------------

def test_near_high_impact_true():
    events = [{"name": "NFP", "impact": "HIGH", "minutes_until": 20}]
    assert _near_high_impact_event(events) is True


def test_near_high_impact_false_time():
    events = [{"name": "NFP", "impact": "HIGH", "minutes_until": 60}]
    assert _near_high_impact_event(events) is False


def test_near_high_impact_low_impact():
    events = [{"name": "some", "impact": "LOW", "minutes_until": 5}]
    assert _near_high_impact_event(events) is False


# ---------------------------------------------------------------------------
# validate_signal
# ---------------------------------------------------------------------------

def test_validate_signal_ok():
    result = validate_signal(_signal(), [], 0.0, 10000, 3, "USD_JPY")
    assert result.ok is True


def test_validate_signal_hold():
    result = validate_signal(_signal("HOLD", 0.9), [], 0.0, 10000, 3, "USD_JPY")
    assert result.ok is False


def test_validate_signal_low_confidence():
    result = validate_signal(_signal("BUY", 0.5), [], 0.0, 10000, 3, "USD_JPY")
    assert result.ok is False


def test_validate_signal_near_event():
    events = [{"name": "NFP", "impact": "HIGH", "minutes_until": 10}]
    result = validate_signal(_signal(), [], 0.0, 10000, 3, "USD_JPY", events)
    assert result.ok is False


def test_validate_signal_daily_loss():
    result = validate_signal(_signal(), [], -10000.0, 10000, 3, "USD_JPY")
    assert result.ok is False


# ---------------------------------------------------------------------------
# _calc_sl_tp
# ---------------------------------------------------------------------------

def test_calc_sl_tp_buy_usdjpy():
    sl, tp = _calc_sl_tp("BUY", 149.50, 30, 60, "USD_JPY")
    assert sl == pytest.approx(149.20, abs=0.001)
    assert tp == pytest.approx(150.10, abs=0.001)


def test_calc_sl_tp_sell_usdjpy():
    sl, tp = _calc_sl_tp("SELL", 149.50, 30, 60, "USD_JPY")
    assert sl == pytest.approx(149.80, abs=0.001)
    assert tp == pytest.approx(148.90, abs=0.001)


# ---------------------------------------------------------------------------
# execute（ペーパートレード）
# ---------------------------------------------------------------------------

def test_execute_paper_trade():
    client = _mock_client()
    sig = _signal("BUY", 0.85)
    with patch("src.trading.order.PAPER_TRADE", True):
        result = execute(client, sig, "USD_JPY")
    assert result.executed is True
    assert result.paper is True
    client.create_market_order.assert_not_called()


def test_execute_skipped_hold():
    client = _mock_client()
    result = execute(client, _signal("HOLD", 0.9), "USD_JPY")
    assert result.executed is False
    client.create_market_order.assert_not_called()


def test_execute_real_order():
    client = _mock_client()
    sig = _signal("BUY", 0.85)
    with patch("src.trading.order.PAPER_TRADE", False):
        result = execute(client, sig, "USD_JPY")
    assert result.executed is True
    assert result.paper is False
    client.create_market_order.assert_called_once()
