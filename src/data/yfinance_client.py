"""yfinance を使ったデータクライアント（開発・テスト用）"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from src.data.base_client import BaseDataClient
from src.data.oanda_client import AccountSummary, Candle, OrderResult, Position

# OANDA形式 → yfinance ティッカー変換
_PAIR_MAP: dict[str, str] = {
    "USD_JPY": "USDJPY=X",
    "EUR_USD": "EURUSD=X",
    "GBP_USD": "GBPUSD=X",
    "EUR_JPY": "EURJPY=X",
    "AUD_USD": "AUDUSD=X",
}

# OANDA粒度 → yfinance interval 変換
_GRANULARITY_MAP: dict[str, str] = {
    "M15": "15m",
    "M30": "30m",
    "H1":  "1h",
    "H4":  "4h",
    "D":   "1d",
}

# yfinance で取得できる最大期間（interval別）
_MAX_PERIOD: dict[str, str] = {
    "15m": "60d",
    "30m": "60d",
    "1h":  "730d",
    "4h":  "730d",
    "1d":  "max",
}


class YFinanceClient(BaseDataClient):
    """
    yfinance ベースのデータクライアント。
    注文はペーパートレード（ログのみ）として扱う。
    """

    def __init__(self, initial_balance: float = 1_000_000.0) -> None:
        self._balance = initial_balance
        self._positions: list[Position] = []

    # ------------------------------------------------------------------
    # 口座情報（仮想）
    # ------------------------------------------------------------------

    def get_account_summary(self) -> AccountSummary:
        unrealized = sum(p.unrealized_pnl for p in self._positions)
        return AccountSummary(
            balance=self._balance,
            unrealized_pnl=unrealized,
            nav=self._balance + unrealized,
            margin_used=0.0,
            margin_available=self._balance,
            currency="JPY",
        )

    # ------------------------------------------------------------------
    # ローソク足
    # ------------------------------------------------------------------

    def get_candles(
        self,
        pair: str,
        granularity: str = "H1",
        count: int = 48,
    ) -> list[Candle]:
        ticker = _PAIR_MAP.get(pair)
        if ticker is None:
            raise ValueError(f"未対応の通貨ペア: {pair}")

        interval = _GRANULARITY_MAP.get(granularity)
        if interval is None:
            raise ValueError(f"未対応の粒度: {granularity}")

        period = _MAX_PERIOD[interval]
        df = yf.download(ticker, period=period, interval=interval, progress=False)

        if df.empty:
            return []

        # 新しい順に並べ直して count 本取得
        df = df.sort_index().tail(count)

        candles = []
        for ts, row in df.iterrows():
            candles.append(Candle(
                time=ts.to_pydatetime().replace(tzinfo=timezone.utc),
                open=float(row["Open"].iloc[0] if hasattr(row["Open"], "iloc") else row["Open"]),
                high=float(row["High"].iloc[0] if hasattr(row["High"], "iloc") else row["High"]),
                low=float(row["Low"].iloc[0] if hasattr(row["Low"], "iloc") else row["Low"]),
                close=float(row["Close"].iloc[0] if hasattr(row["Close"], "iloc") else row["Close"]),
                volume=int(row["Volume"].iloc[0] if hasattr(row["Volume"], "iloc") else row["Volume"]),
            ))
        return candles

    # ------------------------------------------------------------------
    # ポジション（メモリ内で管理）
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list[Position]:
        return list(self._positions)

    # ------------------------------------------------------------------
    # 注文（ペーパートレード）
    # ------------------------------------------------------------------

    def create_market_order(
        self,
        pair: str,
        units: int,
        sl_price: float | None = None,
        tp_price: float | None = None,
    ) -> OrderResult:
        candles = self.get_candles(pair, "H1", 1)
        price = candles[-1].close if candles else 0.0

        trade_id = str(uuid.uuid4())[:8]
        direction = "LONG" if units > 0 else "SHORT"

        self._positions.append(Position(
            trade_id=trade_id,
            instrument=pair,
            direction=direction,
            units=abs(units),
            open_price=price,
            current_price=price,
            unrealized_pnl=0.0,
            open_time=datetime.now(timezone.utc),
        ))

        return OrderResult(
            order_id=str(uuid.uuid4())[:8],
            trade_id=trade_id,
            instrument=pair,
            units=units,
            price=price,
            sl=sl_price,
            tp=tp_price,
            time=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # クローズ（ペーパートレード）
    # ------------------------------------------------------------------

    def close_trade(self, trade_id: str) -> dict[str, Any]:
        self._positions = [p for p in self._positions if p.trade_id != trade_id]
        return {"tradesClosed": [{"tradeID": trade_id}]}
