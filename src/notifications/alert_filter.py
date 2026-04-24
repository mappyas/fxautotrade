"""テクニカル条件アラートフィルター（AIを使わない事前通知）"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.ai.indicators import TechnicalIndicators
from src.notifications.discord import send_discord

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
COOLDOWN_MINUTES = 30
STATE_FILE = Path("data/alert_state.json")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_cooled_down(state: dict, pair: str) -> bool:
    last = state.get(pair, {}).get("last_alert")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return datetime.now(JST) - last_dt >= timedelta(minutes=COOLDOWN_MINUTES)


def _detect_condition(ind: TechnicalIndicators) -> tuple[str, str] | None:
    """
    条件を判定して (condition_key, message) を返す。
    条件なし → None
    """
    rsi   = ind.rsi14
    trend = ind.trend
    sma5  = ind.sma5
    sma20 = ind.sma20

    if rsi is None:
        return None

    # RSI 過買い・過売り（緩め）
    if rsi < 40:
        return ("RSI_OVERSOLD", "🔵 売られすぎ（RSI {:.1f}）→ 買いチャンス候補".format(rsi))
    if rsi > 60:
        return ("RSI_OVERBOUGHT", "🔴 買われすぎ（RSI {:.1f}）→ 売りチャンス候補".format(rsi))

    # 押し目・戻り売り
    if trend == "UP" and rsi <= 50:
        return ("PULLBACK_BUY", "🟢 上昇トレンド中の押し目（RSI {:.1f}）→ 押し目買い候補".format(rsi))
    if trend == "DOWN" and rsi >= 50:
        return ("PULLBACK_SELL", "🟠 下降トレンド中の戻り（RSI {:.1f}）→ 戻り売り候補".format(rsi))

    # SMA5/SMA20 クロス方向
    if sma5 and sma20:
        if sma5 > sma20 and trend != "DOWN":
            return ("SMA_BULL", "📈 SMA5 > SMA20（RSI {:.1f}）→ 買い方向".format(rsi))
        if sma5 < sma20 and trend != "UP":
            return ("SMA_BEAR", "📉 SMA5 < SMA20（RSI {:.1f}）→ 売り方向".format(rsi))

    return None


def check_and_notify(
    pair: str,
    ind: TechnicalIndicators,
    webhook_url: str,
) -> str | None:
    """
    条件チェックして通知。発火した condition_key を返す（なければ None）。
    """
    result = _detect_condition(ind)
    if result is None:
        return None

    condition_key, detail = result
    state = _load_state()

    if not _is_cooled_down(state, pair):
        logger.debug("%s: クールダウン中のためスキップ", pair)
        return None

    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    pair_label = pair.replace("_", "/")
    message = (
        f"**【FXアラート】{pair_label}**\n"
        f"{detail}\n"
        f"SMA20: {ind.sma20}　SMA50: {ind.sma50}　トレンド: {ind.trend}\n"
        f"⏰ {now_str}"
    )

    send_discord(webhook_url, message)

    state[pair] = {
        "last_alert": datetime.now(JST).isoformat(),
        "last_condition": condition_key,
    }
    _save_state(state)
    logger.info("%s: アラート送信 (%s)", pair, condition_key)
    return condition_key
