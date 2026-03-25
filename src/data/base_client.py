"""データクライアントの抽象基底クラス"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.data.oanda_client import AccountSummary, Candle, OrderResult, Position


class BaseDataClient(ABC):

    @abstractmethod
    def get_account_summary(self) -> AccountSummary:
        """口座情報を取得する"""

    @abstractmethod
    def get_candles(
        self,
        pair: str,
        granularity: str = "H1",
        count: int = 48,
    ) -> list[Candle]:
        """ローソク足を取得する"""

    def get_multi_granularity_candles(
        self,
        pair: str,
        granularity_counts: dict[str, int],
    ) -> dict[str, list[Candle]]:
        """複数時間足を一括取得する（デフォルト実装）"""
        return {
            gran: self.get_candles(pair, gran, count)
            for gran, count in granularity_counts.items()
        }

    @abstractmethod
    def get_open_positions(self) -> list[Position]:
        """オープンポジション一覧を取得する"""

    @abstractmethod
    def create_market_order(
        self,
        pair: str,
        units: int,
        sl_price: float | None = None,
        tp_price: float | None = None,
    ) -> OrderResult:
        """成行注文を送信する"""

    @abstractmethod
    def close_trade(self, trade_id: str) -> dict[str, Any]:
        """ポジションをクローズする"""
