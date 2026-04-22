"""Discord Webhook 通知モジュール"""
from __future__ import annotations

import logging
import httpx

logger = logging.getLogger(__name__)


def send_discord(webhook_url: str, message: str) -> None:
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL が未設定のため通知をスキップ")
        return
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(webhook_url, json={"content": message})
            resp.raise_for_status()
    except Exception as e:
        logger.warning("Discord通知失敗: %s", e)
