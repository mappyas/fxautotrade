"""リスク管理モジュール"""
from __future__ import annotations

from dataclasses import dataclass

from src.data.oanda_client import Position


# pip値（通貨ペアごとの1unit・1pipあたりのJPY価値）
_PIP_SIZE: dict[str, float] = {
    "USD_JPY": 0.01,    # 1pip = 0.01円
    "EUR_JPY": 0.01,
    "GBP_JPY": 0.01,
    "EUR_USD": 0.0001,  # USD建て → 要JPY換算（簡易実装では近似）
    "GBP_USD": 0.0001,
    "AUD_USD": 0.0001,
}


@dataclass
class RiskCheckResult:
    ok: bool
    reason: str = ""


def calc_position_size(
    balance: float,
    risk_pct: float,
    sl_pips: int,
    pair: str,
    current_price: float = 1.0,
) -> int:
    """
    1トレードあたりのユニット数を計算する。

    計算式:
      リスク金額 = 残高 × (risk_pct / 100)
      ユニット数 = リスク金額 / (SL_pips × pip値)

    JPYクロス（USD_JPY等）はそのままJPY計算。
    USD建てペア（EUR_USD等）はcurrent_priceでJPY換算。
    """
    if sl_pips <= 0:
        return 0

    pip_size = _PIP_SIZE.get(pair, 0.01)
    risk_amount = balance * (risk_pct / 100)

    # USD建てペアは現在レートでJPYに換算（簡易: USDJPY≈150と仮定）
    pip_value_jpy = pip_size if pair.endswith("JPY") else pip_size * current_price

    units = risk_amount / (sl_pips * pip_value_jpy)
    # 1000単位に切り捨て（最小ロット）
    return max(1000, int(units // 1000) * 1000)


def check_daily_loss_limit(daily_pnl: float, max_loss: float) -> RiskCheckResult:
    """
    日次損失が上限に達していないか確認する。
    daily_pnl が負の値で累積損失を表す。
    """
    if daily_pnl <= -abs(max_loss):
        return RiskCheckResult(
            ok=False,
            reason=f"日次損失上限に達しました（損失: {daily_pnl:.0f}円 / 上限: {-abs(max_loss):.0f}円）",
        )
    return RiskCheckResult(ok=True)


def check_max_positions(
    open_positions: list[Position],
    max_count: int,
    pair: str | None = None,
) -> RiskCheckResult:
    """
    最大同時ポジション数を超えていないか確認する。
    pair を指定すると同一ペアの既存ポジションもチェックする。
    """
    if len(open_positions) >= max_count:
        return RiskCheckResult(
            ok=False,
            reason=f"最大ポジション数に達しています（{len(open_positions)}/{max_count}）",
        )

    if pair:
        same_pair = [p for p in open_positions if p.instrument == pair]
        if same_pair:
            return RiskCheckResult(
                ok=False,
                reason=f"{pair} のポジションが既に存在します",
            )

    return RiskCheckResult(ok=True)


def validate_all(
    open_positions: list[Position],
    daily_pnl: float,
    max_daily_loss: float,
    max_positions: int,
    pair: str,
) -> RiskCheckResult:
    """全リスクチェックをまとめて実行する"""
    for check in [
        check_daily_loss_limit(daily_pnl, max_daily_loss),
        check_max_positions(open_positions, max_positions, pair),
    ]:
        if not check.ok:
            return check
    return RiskCheckResult(ok=True)
