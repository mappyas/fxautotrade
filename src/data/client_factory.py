"""DATA_SOURCE 設定に応じてクライアントを返すファクトリ"""
from __future__ import annotations

from src.data.base_client import BaseDataClient


def get_data_client() -> BaseDataClient:
    from src.config import DATA_SOURCE

    if DATA_SOURCE == "yfinance":
        from src.data.yfinance_client import YFinanceClient
        return YFinanceClient()

    if DATA_SOURCE == "oanda":
        from src.data.oanda_client import OandaClient
        return OandaClient()

    raise ValueError(f"未対応の DATA_SOURCE: {DATA_SOURCE}")
