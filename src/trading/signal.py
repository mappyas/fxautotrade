"""AIシグナルの検証・フィルタリング"""
from __future__ import annotations

from dataclasses import dataclass

from src.ai.analyzer import Signal
from src.data.oanda_client import Position
from src.trading.risk import RiskCheckResult, validate_all


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def validate_signal(
    signal: Signal,
    open_positions: list[Position],
    daily_pnl: float,
    max_daily_loss: float,
    max_positions: int,
    pair: str,
    economic_events: list[dict] | None = None,
) -> ValidationResult:
    """
    AIシグナルを受け取り、取引可能かどうかを判断する。
    全チェックをパスした場合のみ ok=True を返す。
    """
    # 1. シグナル自体が実行可能か
    if not signal.is_actionable:
        return ValidationResult(
            ok=False,
            reason=f"シグナル不採用 (action={signal.action}, confidence={signal.confidence:.2f})",
        )

    # 2. 重要経済指標の30分前はスキップ
    if _near_high_impact_event(economic_events or []):
        return ValidationResult(
            ok=False,
            reason="重要経済指標の直前のため取引をスキップ",
        )

    # 3. リスク管理チェック
    risk = validate_all(open_positions, daily_pnl, max_daily_loss, max_positions, pair)
    if not risk.ok:
        return ValidationResult(ok=False, reason=risk.reason)

    return ValidationResult(ok=True)


def _near_high_impact_event(events: list[dict], minutes: int = 30) -> bool:
    """
    直近 minutes 分以内に HIGH インパクトの経済指標があるか判定する。
    events の各要素は {"name": str, "impact": str, "minutes_until": int} を期待する。
    """
    for event in events:
        if event.get("impact") == "HIGH":
            minutes_until = event.get("minutes_until", 9999)
            if abs(minutes_until) <= minutes:
                return True
    return False
