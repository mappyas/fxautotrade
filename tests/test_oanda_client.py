"""OandaClient のユニットテスト（OANDA API をモック）"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.data.oanda_client import AccountSummary, Candle, OandaClient, OrderResult, Position


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    with patch("src.data.oanda_client.oandapyV20.API"):
        c = OandaClient()
        c._client = MagicMock()
        return c


# ---------------------------------------------------------------------------
# get_account_summary
# ---------------------------------------------------------------------------

def test_get_account_summary(client: OandaClient):
    client._client.request.return_value = {
        "account": {
            "balance": "1000000.00",
            "unrealizedPL": "-5000.00",
            "NAV": "995000.00",
            "marginUsed": "20000.00",
            "marginAvailable": "975000.00",
            "currency": "JPY",
        }
    }
    result = client.get_account_summary()
    assert isinstance(result, AccountSummary)
    assert result.balance == 1_000_000.0
    assert result.currency == "JPY"
    assert result.nav == 995_000.0


# ---------------------------------------------------------------------------
# get_candles
# ---------------------------------------------------------------------------

def _make_candle_resp(n: int) -> dict:
    candles = []
    for i in range(n):
        candles.append({
            "time": f"2024-01-{i+1:02d}T00:00:00Z",
            "complete": True,
            "volume": 1000,
            "mid": {"o": "149.50", "h": "150.00", "l": "149.00", "c": "149.80"},
        })
    return {"candles": candles}


def test_get_candles_returns_correct_count(client: OandaClient):
    client._client.request.return_value = _make_candle_resp(10)
    result = client.get_candles("USD_JPY", "H1", 10)
    assert len(result) == 10
    assert isinstance(result[0], Candle)


def test_get_candles_parses_ohlcv(client: OandaClient):
    client._client.request.return_value = _make_candle_resp(1)
    candle = client.get_candles("USD_JPY", "H1", 1)[0]
    assert candle.open  == 149.50
    assert candle.high  == 150.00
    assert candle.low   == 149.00
    assert candle.close == 149.80
    assert candle.volume == 1000


def test_get_candles_skips_incomplete(client: OandaClient):
    client._client.request.return_value = {
        "candles": [
            {"time": "2024-01-01T00:00:00Z", "complete": True,  "volume": 100,
             "mid": {"o": "149.50", "h": "150.00", "l": "149.00", "c": "149.80"}},
            {"time": "2024-01-01T01:00:00Z", "complete": False, "volume": 50,
             "mid": {"o": "149.80", "h": "149.90", "l": "149.70", "c": "149.85"}},
        ]
    }
    result = client.get_candles("USD_JPY", "H1", 2)
    assert len(result) == 1


def test_get_multi_granularity_candles(client: OandaClient):
    client._client.request.return_value = _make_candle_resp(5)
    result = client.get_multi_granularity_candles("USD_JPY", {"H1": 5, "H4": 5})
    assert set(result.keys()) == {"H1", "H4"}
    assert len(result["H1"]) == 5


# ---------------------------------------------------------------------------
# get_open_positions
# ---------------------------------------------------------------------------

def test_get_open_positions_long(client: OandaClient):
    client._client.request.return_value = {
        "positions": [
            {
                "instrument": "USD_JPY",
                "long":  {"units": "10000", "averagePrice": "149.50",
                           "unrealizedPL": "5000", "tradeIDs": ["101"]},
                "short": {"units": "0", "averagePrice": "0",
                           "unrealizedPL": "0", "tradeIDs": []},
            }
        ]
    }
    result = client.get_open_positions()
    assert len(result) == 1
    assert result[0].direction == "LONG"
    assert result[0].units == 10000
    assert result[0].instrument == "USD_JPY"


def test_get_open_positions_empty(client: OandaClient):
    client._client.request.return_value = {"positions": []}
    assert client.get_open_positions() == []


# ---------------------------------------------------------------------------
# create_market_order
# ---------------------------------------------------------------------------

def test_create_market_order_buy(client: OandaClient):
    client._client.request.return_value = {
        "orderFillTransaction": {
            "orderID": "1001",
            "tradeOpened": {"tradeID": "2001"},
            "price": "149.50",
        }
    }
    result = client.create_market_order("USD_JPY", units=10000, sl_price=148.50, tp_price=151.00)
    assert isinstance(result, OrderResult)
    assert result.units == 10000
    assert result.trade_id == "2001"
    assert result.sl == 148.50
    assert result.tp == 151.00


def test_create_market_order_sell(client: OandaClient):
    client._client.request.return_value = {
        "orderFillTransaction": {
            "orderID": "1002",
            "tradeOpened": {"tradeID": "2002"},
            "price": "149.50",
        }
    }
    result = client.create_market_order("USD_JPY", units=-10000)
    assert result.units == -10000
