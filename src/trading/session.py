"""取引セッション判定"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


@dataclass
class SessionInfo:
    name: str           # "東京" | "ロンドン" | "NY・米指標" | "NY後半" | "閑散"
    recommended: bool   # 取引推奨かどうか
    caution: bool       # 注意（取引はできるがボラ高）
    reason: str         # 理由


# USD/JPY向けセッション定義（JST時間帯）
# (start_hour_inclusive, end_hour_exclusive, SessionInfo)
_USDJPY_SESSIONS: list[tuple[int, int, SessionInfo]] = [
    (9,  15, SessionInfo("東京",       recommended=True,  caution=False, reason="東京時間は流動性が高く安定")),
    (15, 18, SessionInfo("ロンドン前半", recommended=True,  caution=False, reason="ロンドン勢参入でトレンド発生しやすい")),
    (18, 21, SessionInfo("ロンドン後半", recommended=True,  caution=False, reason="欧米時間のオーバーラップ")),
    (21, 24, SessionInfo("NY・米指標",  recommended=False, caution=True,  reason="米経済指標・FOMC等でボラ急騰リスク")),
    (0,   2, SessionInfo("NY後半",     recommended=False, caution=True,  reason="流動性低下と突発的な値動きに注意")),
    (2,   9, SessionInfo("閑散",       recommended=False, caution=False, reason="流動性が低くスリッページリスク大")),
]

# EUR/USD向けセッション定義（JST時間帯）
_EURUSD_SESSIONS: list[tuple[int, int, SessionInfo]] = [
    (9,  15, SessionInfo("東京",       recommended=False, caution=False, reason="EUR/USDは東京時間は動きが少ない")),
    (15, 18, SessionInfo("ロンドン前半", recommended=True,  caution=False, reason="ロンドン勢参入でEUR/USDが動き出す")),
    (18, 23, SessionInfo("ロンドン/NY", recommended=True,  caution=False, reason="欧米オーバーラップで最も流動性が高い")),
    (23, 24, SessionInfo("NY後半",     recommended=False, caution=True,  reason="流動性低下")),
    (0,   2, SessionInfo("NY後半",     recommended=False, caution=True,  reason="流動性低下")),
    (2,   9, SessionInfo("閑散",       recommended=False, caution=False, reason="流動性が低い")),
]

_SESSION_MAP: dict[str, list[tuple[int, int, SessionInfo]]] = {
    "USD_JPY": _USDJPY_SESSIONS,
    "EUR_USD": _EURUSD_SESSIONS,
}


def get_session(pair: str, dt: datetime | None = None) -> SessionInfo:
    """指定時刻（デフォルト=現在）のセッション情報を返す"""
    if dt is None:
        dt = datetime.now(JST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(JST)
    else:
        dt = dt.astimezone(JST)

    hour = dt.hour
    sessions = _SESSION_MAP.get(pair, _USDJPY_SESSIONS)

    for start, end, info in sessions:
        if start <= hour < end:
            return info

    # フォールバック（通常はここに来ない）
    return SessionInfo("不明", recommended=False, caution=True, reason="セッション判定不可")
