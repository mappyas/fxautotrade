"""YFinanceClient のユニットテスト（yfinance をモック）"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.yfinance_client import YFinanceClient
from src.data.oanda_client import Candle, AccountSummary, OrderResult


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_df(n: int = 3) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    data = {
        ("Open",   "USDJPY=X"): [149.0] * n,
        ("High",   "USDJPY=X"): [150.0] * n,
        ("Low",    "USDJPY=X"): [148.5] * n,
        ("Close",  "USDJPY=X"): [149.5] * n,
        ("Volume", "USDJPY=X"): [1000]  * n,
    }
    return pd.DataFrame(data, index=idx)


# ---------------------------------------------------------------------------
# get_account_summary
# ---------------------------------------------------------------------------

def test_get_account_summary_initial():
    client = YFinanceClient(initial_balance=1_000_000.0)
    summary = client.get_account_summary()
    assert isinstance(summary, AccountSummary)
    assert summary.balance == 1_000_000.0
    assert summary.currency == "JPY"
    assert summary.unrealized_pnl == 0.0


# ---------------------------------------------------------------------------
# get_candles
# ---------------------------------------------------------------------------

@patch("src.data.yfinance_client.yf.download")
def test_get_candles_returns_candles(mock_dl):
    mock_dl.return_value = _make_df(5)
    client = YFinanceClient()
    candles = client.get_candles("USD_JPY", "H1", 5)
    assert len(candles) == 5
    assert isinstance(candles[0], Candle)


@patch("src.data.yfinance_client.yf.download")
def test_get_candles_ohlcv_values(mock_dl):
    mock_dl.return_value = _make_df(1)
    client = YFinanceClient()
    candle = client.get_candles("USD_JPY", "H1", 1)[0]
    assert candle.open  == 149.0
    assert candle.high  == 150.0
    assert candle.low   == 148.5
    assert candle.close == 149.5
    assert candle.volume == 1000


@patch("src.data.yfinance_client.yf.download")
def test_get_candles_empty(mock_dl):
    mock_dl.return_value = pd.DataFrame()
    client = YFinanceClient()
    assert client.get_candles("USD_JPY", "H1", 5) == []


def test_get_candles_unsupported_pair():
    client = YFinanceClient()
    with pytest.raises(ValueError, match="未対応の通貨ペア"):
        client.get_candles("XYZ_ABC", "H1", 5)


def test_get_candles_unsupported_granularity():
    client = YFinanceClient()
    with pytest.raises(ValueError, match="未対応の粒度"):
        client.get_candles("USD_JPY", "W1", 5)


# ---------------------------------------------------------------------------
# get_open_positions / create_market_order / close_trade
# ---------------------------------------------------------------------------

@patch("src.data.yfinance_client.yf.download")
def test_create_market_order_adds_position(mock_dl):
    mock_dl.return_value = _make_df(1)
    client = YFinanceClient()
    assert len(client.get_open_positions()) == 0

    result = client.create_market_order("USD_JPY", units=10000, sl_price=148.0, tp_price=151.0)
    assert isinstance(result, OrderResult)
    assert result.units == 10000
    assert len(client.get_open_positions()) == 1
    assert client.get_open_positions()[0].direction == "LONG"


@patch("src.data.yfinance_client.yf.download")
def test_create_market_order_short(mock_dl):
    mock_dl.return_value = _make_df(1)
    client = YFinanceClient()
    client.create_market_order("USD_JPY", units=-10000)
    assert client.get_open_positions()[0].direction == "SHORT"


@patch("src.data.yfinance_client.yf.download")
def test_close_trade_removes_position(mock_dl):
    mock_dl.return_value = _make_df(1)
    client = YFinanceClient()
    result = client.create_market_order("USD_JPY", units=10000)
    trade_id = result.trade_id

    client.close_trade(trade_id)
    assert len(client.get_open_positions()) == 0


# ---------------------------------------------------------------------------
# client_factory
# ---------------------------------------------------------------------------

def test_client_factory_yfinance():
    with patch.dict("os.environ", {"DATA_SOURCE": "yfinance"}):
        from importlib import reload
        import src.config as cfg
        reload(cfg)
        from src.data.client_factory import get_data_client
        client = get_data_client()
        assert isinstance(client, YFinanceClient)
