"""OANDA v20 API ラッパー"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import oandapyV20
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.trades as trades
from oandapyV20.contrib.requests import MarketOrderRequest, TakeProfitDetails, StopLossDetails

from src.config import OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_ENVIRONMENT


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class AccountSummary:
    balance: float
    unrealized_pnl: float
    nav: float          # Net Asset Value
    margin_used: float
    margin_available: float
    currency: str


@dataclass
class Position:
    trade_id: str
    instrument: str
    direction: str      # "LONG" | "SHORT"
    units: int
    open_price: float
    current_price: float
    unrealized_pnl: float
    open_time: datetime


@dataclass
class OrderResult:
    order_id: str
    trade_id: str
    instrument: str
    units: int
    price: float
    sl: float | None
    tp: float | None
    time: datetime


class OandaClient:
    """OANDA v20 REST API ブローカークライアント"""

    def __init__(self) -> None:
        self._client = oandapyV20.API(
            access_token=OANDA_API_KEY,
            environment=OANDA_ENVIRONMENT,
        )
        self._account_id = OANDA_ACCOUNT_ID

    # ------------------------------------------------------------------
    # 口座情報
    # ------------------------------------------------------------------

    def get_account_summary(self) -> AccountSummary:
        req = accounts.AccountSummary(self._account_id)
        resp = self._client.request(req)
        a = resp["account"]
        return AccountSummary(
            balance=float(a["balance"]),
            unrealized_pnl=float(a["unrealizedPL"]),
            nav=float(a["NAV"]),
            margin_used=float(a["marginUsed"]),
            margin_available=float(a["marginAvailable"]),
            currency=a["currency"],
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
        """
        pair        : "USD_JPY" など
        granularity : "M15" | "H1" | "H4" | "D"
        count       : 取得本数
        """
        params = {"granularity": granularity, "count": count}
        req = instruments.InstrumentsCandles(instrument=pair, params=params)
        resp = self._client.request(req)

        result = []
        for c in resp["candles"]:
            if not c["complete"]:
                continue
            mid = c["mid"]
            result.append(Candle(
                time=datetime.fromisoformat(c["time"].replace("Z", "+00:00")),
                open=float(mid["o"]),
                high=float(mid["h"]),
                low=float(mid["l"]),
                close=float(mid["c"]),
                volume=int(c["volume"]),
            ))
        return result

    def get_multi_granularity_candles(
        self,
        pair: str,
        granularity_counts: dict[str, int],
    ) -> dict[str, list[Candle]]:
        """複数時間足を一括取得"""
        return {
            gran: self.get_candles(pair, gran, count)
            for gran, count in granularity_counts.items()
        }

    # ------------------------------------------------------------------
    # ポジション
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list[Position]:
        req = positions.OpenPositions(self._account_id)
        resp = self._client.request(req)

        result = []
        for p in resp["positions"]:
            instrument = p["instrument"]
            for side, direction in (("long", "LONG"), ("short", "SHORT")):
                side_data = p[side]
                if int(side_data["units"]) == 0:
                    continue
                for trade_id in side_data.get("tradeIDs", []):
                    result.append(Position(
                        trade_id=trade_id,
                        instrument=instrument,
                        direction=direction,
                        units=abs(int(side_data["units"])),
                        open_price=float(side_data["averagePrice"]),
                        current_price=0.0,   # 必要なら別途取得
                        unrealized_pnl=float(side_data["unrealizedPL"]),
                        open_time=datetime.now(timezone.utc),
                    ))
        return result

    # ------------------------------------------------------------------
    # 注文
    # ------------------------------------------------------------------

    def create_market_order(
        self,
        pair: str,
        units: int,          # 正=買い、負=売り
        sl_price: float | None = None,
        tp_price: float | None = None,
    ) -> OrderResult:
        """
        成行注文を送信する。
        units > 0 : 買い（ロング）
        units < 0 : 売り（ショート）
        """
        sl_detail = StopLossDetails(price=str(round(sl_price, 5))) if sl_price else None
        tp_detail = TakeProfitDetails(price=str(round(tp_price, 5))) if tp_price else None

        order_body = MarketOrderRequest(
            instrument=pair,
            units=units,
            stopLossOnFill=sl_detail,
            takeProfitOnFill=tp_detail,
        )
        req = orders.OrderCreate(self._account_id, data=order_body.data)
        resp = self._client.request(req)

        filled = resp.get("orderFillTransaction", {})
        return OrderResult(
            order_id=filled.get("orderID", ""),
            trade_id=filled.get("tradeOpened", {}).get("tradeID", ""),
            instrument=pair,
            units=units,
            price=float(filled.get("price", 0)),
            sl=sl_price,
            tp=tp_price,
            time=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # ポジションクローズ
    # ------------------------------------------------------------------

    def close_trade(self, trade_id: str) -> dict[str, Any]:
        req = trades.TradeClose(self._account_id, tradeID=trade_id)
        return self._client.request(req)

    # ------------------------------------------------------------------
    # 現在値取得（pip計算用）
    # ------------------------------------------------------------------

    def get_current_price(self, pair: str) -> tuple[float, float]:
        """(bid, ask) を返す"""
        params = {"granularity": "S5", "count": 1}
        req = instruments.InstrumentsCandles(instrument=pair, params=params)
        resp = self._client.request(req)
        candle = resp["candles"][-1]
        bid = float(candle["bid"]["c"]) if "bid" in candle else float(candle["mid"]["c"])
        ask = float(candle["ask"]["c"]) if "ask" in candle else float(candle["mid"]["c"])
        return bid, ask
