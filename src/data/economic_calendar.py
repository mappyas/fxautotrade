"""Finnhub 経済カレンダー取得モジュール"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# FX に関連する主要国コード
_TARGET_COUNTRIES = {"US", "JP", "EU", "GB", "AU", "CA", "CH", "NZ"}


def fetch_economic_events(api_key: str, days_ahead: int = 1) -> list[dict]:
    """
    今日〜days_ahead日後の経済指標を取得し、影響度でフィルタして返す。

    Returns:
        list of dict with keys: event, country, impact, time, actual, forecast, prev
    """
    if not api_key:
        logger.warning("FINNHUB_API_KEY が未設定のため経済指標をスキップ")
        return []

    today = datetime.now(JST).date()
    to_date = today + timedelta(days=days_ahead)

    url = "https://finnhub.io/api/v1/calendar/economic"
    params = {
        "from":  today.strftime("%Y-%m-%d"),
        "to":    to_date.strftime("%Y-%m-%d"),
        "token": api_key,
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Finnhub HTTPエラー: %s", e)
        return []
    except Exception as e:
        logger.warning("Finnhub取得失敗: %s", e)
        return []

    events = data.get("economicCalendar", [])

    # 対象国 + high/medium のみに絞る
    filtered = [
        {
            "event":    e.get("event", ""),
            "country":  e.get("country", ""),
            "impact":   e.get("impact", "low"),
            "time":     e.get("time", ""),
            "actual":   e.get("actual"),
            "forecast": e.get("estimate"),
            "prev":     e.get("prev"),
        }
        for e in events
        if e.get("country", "") in _TARGET_COUNTRIES
        and e.get("impact", "low") in ("high", "medium")
    ]

    logger.info("経済指標取得: %d件 (high/medium, 対象国)", len(filtered))
    return filtered
