"""AI モジュールのユニットテスト"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.ai.indicators import TechnicalIndicators, calc_indicators
from src.ai.analyzer import Signal, _parse_response, _select_model
from src.data.oanda_client import Candle


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_candles(n: int, base_price: float = 149.0) -> list[Candle]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(
            time=base + timedelta(hours=i),
            open=base_price, high=base_price + 0.5,
            low=base_price - 0.5, close=base_price + (i * 0.01),
            volume=1000,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------

def test_calc_indicators_empty():
    result = calc_indicators([])
    assert result.trend == "FLAT"
    assert result.sma20 is None


def test_calc_indicators_sma():
    candles = _make_candles(55)
    result = calc_indicators(candles)
    assert result.sma20 is not None
    assert result.sma50 is not None


def test_calc_indicators_rsi_range():
    candles = _make_candles(30)
    result = calc_indicators(candles)
    if result.rsi14 is not None:
        assert 0 <= result.rsi14 <= 100


def test_calc_indicators_insufficient_data():
    candles = _make_candles(10)
    result = calc_indicators(candles)
    assert result.sma20 is None   # 10本では20MA計算不可
    assert result.sma50 is None


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

def test_parse_response_with_codeblock():
    content = '''```json
{
  "action": "BUY",
  "confidence": 0.8,
  "timeframe": "DAY_TRADE",
  "suggested_sl_pips": 25,
  "suggested_tp_pips": 50,
  "reasoning": "上昇トレンド継続中"
}
```'''
    signal = _parse_response(content)
    assert signal.action == "BUY"
    assert signal.confidence == 0.8
    assert signal.suggested_sl_pips == 25
    assert signal.reasoning == "上昇トレンド継続中"


def test_parse_response_raw_json():
    content = '{"action": "SELL", "confidence": 0.72, "timeframe": "SWING", "suggested_sl_pips": 40, "suggested_tp_pips": 80, "reasoning": "下降トレンド"}'
    signal = _parse_response(content)
    assert signal.action == "SELL"
    assert signal.timeframe == "SWING"


def test_parse_response_invalid_json():
    signal = _parse_response("これはJSONではない")
    assert signal.action == "HOLD"
    assert signal.confidence == 0.0


def test_parse_response_hold():
    content = '{"action": "HOLD", "confidence": 0.55, "timeframe": "DAY_TRADE", "suggested_sl_pips": 30, "suggested_tp_pips": 60, "reasoning": "不確実"}'
    signal = _parse_response(content)
    assert signal.action == "HOLD"
    assert not signal.is_actionable


# ---------------------------------------------------------------------------
# Signal.is_actionable
# ---------------------------------------------------------------------------

def test_signal_is_actionable_true():
    s = Signal("BUY", 0.80, "DAY_TRADE", 30, 60, "test")
    assert s.is_actionable is True


def test_signal_is_actionable_low_confidence():
    s = Signal("BUY", 0.65, "DAY_TRADE", 30, 60, "test")
    assert s.is_actionable is False


def test_signal_is_actionable_hold():
    s = Signal("HOLD", 0.90, "DAY_TRADE", 30, 60, "test")
    assert s.is_actionable is False


# ---------------------------------------------------------------------------
# _select_model
# ---------------------------------------------------------------------------

def test_select_model_normal():
    from src.config import PRIMARY_MODEL
    indicators = TechnicalIndicators(None, None, 50.0, 0.3, "FLAT")
    model = _select_model(indicators, [])
    assert model == PRIMARY_MODEL


def test_select_model_high_impact_event():
    from src.config import FALLBACK_MODEL
    indicators = TechnicalIndicators(None, None, 50.0, 0.3, "FLAT")
    events = [{"name": "NFP", "impact": "HIGH"}]
    model = _select_model(indicators, events)
    assert model == FALLBACK_MODEL


# ---------------------------------------------------------------------------
# analyze（Groq APIをモック）
# ---------------------------------------------------------------------------

@patch("src.ai.analyzer._call_groq")
def test_analyze_returns_signal(mock_groq):
    mock_groq.return_value = '{"action": "BUY", "confidence": 0.82, "timeframe": "DAY_TRADE", "suggested_sl_pips": 30, "suggested_tp_pips": 60, "reasoning": "上昇トレンド"}'

    from src.ai.analyzer import analyze
    candles = _make_candles(55)
    signal = analyze("USD_JPY", candles, candles, candles, [])

    assert signal.action == "BUY"
    assert signal.confidence == 0.82
    assert signal.model_used != ""


@patch("src.ai.analyzer._call_groq")
def test_analyze_fallback_on_borderline_confidence(mock_groq):
    # 1回目: 境界値 confidence → 2回目: fallback モデルで再判断
    mock_groq.side_effect = [
        '{"action": "BUY", "confidence": 0.68, "timeframe": "DAY_TRADE", "suggested_sl_pips": 30, "suggested_tp_pips": 60, "reasoning": "微妙"}',
        '{"action": "BUY", "confidence": 0.78, "timeframe": "DAY_TRADE", "suggested_sl_pips": 30, "suggested_tp_pips": 60, "reasoning": "確信あり"}',
    ]

    from src.ai.analyzer import analyze
    candles = _make_candles(55)
    signal = analyze("USD_JPY", candles, candles, candles, [])

    assert signal.fallback_used is True
    assert mock_groq.call_count == 2
