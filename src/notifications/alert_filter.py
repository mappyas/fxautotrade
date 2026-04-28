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

    優先順位:
      1. MACDクロス + SMA5/20同方向（精度重視）
      2. RSI極値 + SMA確認（補助）
    """
    sma5       = ind.sma5
    sma20      = ind.sma20
    rsi        = ind.rsi14
    hist       = ind.macd_hist
    hist_prev  = ind.macd_hist_prev

    if sma5 is None or sma20 is None:
        return None

    sma_bull = sma5 > sma20   # SMA5がSMA20の上
    sma_bear = sma5 < sma20   # SMA5がSMA20の下

    # --- 1. MACDクロス + SMA同方向 ---
    if hist is not None and hist_prev is not None:
        macd_cross_up   = hist_prev < 0 and hist >= 0   # ゴールデンクロス
        macd_cross_down = hist_prev > 0 and hist <= 0   # デッドクロス

        if macd_cross_up and sma_bull:
            return ("MACD_BULL", "📈 MACDゴールデンクロス + SMA5>SMA20 → 買いシグナル")
        if macd_cross_down and sma_bear:
            return ("MACD_BEAR", "📉 MACDデッドクロス + SMA5<SMA20 → 売りシグナル")

    # --- 2. RSI極値 + SMA確認 ---
    if rsi is not None:
        if rsi < 35 and sma_bull:
            return ("RSI_OVERSOLD", "🔵 売られすぎ（RSI {:.1f}）+ 上方向SMA → 買い候補".format(rsi))
        if rsi > 65 and sma_bear:
            return ("RSI_OVERBOUGHT", "🔴 買われすぎ（RSI {:.1f}）+ 下方向SMA → 売り候補".format(rsi))

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
