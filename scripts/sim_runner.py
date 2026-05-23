"""
FX 自動売買シミュレーター（PDCAサイクル統合版）

使い方:
    python scripts/sim_runner.py

動作:
    - 1分ごとにポーリング（09:00〜24:00 JST）
    - Plan（plan_state.json）のbias + テクニカル条件でエントリー判断
    - TP/SL到達または逆シグナルで自動決済
    - 決済後に自動でCheckフェーズを実行
    - 結果を data/sim_results.json に保存
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/sim_runner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
JST = timezone(timedelta(hours=9))

TRADE_START_HOUR = 9   # 取引開始（JST）
TRADE_END_HOUR   = 24  # 取引終了（JST）

POLL_INTERVAL_SEC = 60
SL_PIPS = 12   # スキャルデフォルト
TP_PIPS = 24   # スキャルデフォルト

PAIRS = ["USD_JPY"]

RESULTS_FILE = Path("data/sim_results.json")
STATE_FILE   = Path("data/sim_state.json")

CONDITION_DIRECTION = {
    "MACD_BULL":      "BUY",
    "RSI_OVERSOLD":   "BUY",
    "MACD_BEAR":      "SELL",
    "RSI_OVERBOUGHT": "SELL",
}


# ------------------------------------------------------------------
# 状態管理
# ------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"open_trade": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_results() -> list:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    return []


def save_result(trade: dict) -> None:
    results = load_results()
    results.append(trade)
    RESULTS_FILE.parent.mkdir(exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# pip 計算
# ------------------------------------------------------------------

def pip_value(pair: str) -> float:
    return 0.01 if pair.endswith("JPY") else 0.0001


def calc_sl_tp(pair: str, direction: str, entry: float, sl_pips: int, tp_pips: int) -> tuple[float, float]:
    pip = pip_value(pair)
    if direction == "BUY":
        return entry - sl_pips * pip, entry + tp_pips * pip
    else:
        return entry + sl_pips * pip, entry - tp_pips * pip


# ------------------------------------------------------------------
# Planフィルター
# ------------------------------------------------------------------

def _get_plan(pair: str) -> dict:
    from src.ai.planner import load_plan_state
    return load_plan_state().get(pair, {})


def _plan_allows_entry(plan: dict, direction: str, now: datetime) -> tuple[bool, str]:
    """Planのbias・avoid_untilでエントリー可否を判断する"""
    avoid_until_str = plan.get("avoid_until")
    if avoid_until_str:
        try:
            avoid_until = datetime.fromisoformat(avoid_until_str)
            if now < avoid_until:
                return False, f"エントリー禁止期間中（〜{avoid_until.strftime('%H:%M')}）"
        except Exception:
            pass

    bias = plan.get("bias", "NEUTRAL")
    if bias == "SELL" and direction == "BUY":
        return False, f"Plan bias={bias} のためBUYスキップ"
    if bias == "BUY" and direction == "SELL":
        return False, f"Plan bias={bias} のためSELLスキップ"

    return True, ""


def _get_sl_tp_pips(plan: dict, direction: str) -> tuple[int, int]:
    """Plan AのSL/TPを取得する。なければデフォルト"""
    for p in plan.get("plans", []):
        if p.get("label") == "A" and p.get("entry") == direction:
            sl = p.get("sl_pips")
            tp = p.get("tp_pips")
            if sl and tp:
                return int(sl), int(tp)
    return SL_PIPS, TP_PIPS


# ------------------------------------------------------------------
# メインループ
# ------------------------------------------------------------------

def run() -> None:
    from src.ai.indicators import calc_indicators
    from src.data.client_factory import get_data_client
    from src.notifications.alert_filter import _detect_condition

    Path("data").mkdir(exist_ok=True)
    client = get_data_client()

    logger.info("=== シミュレーター開始（PDCAモード）===")
    logger.info("取引時間: %d:00〜%d:00 JST / SL=%dpips TP=%dpips",
                TRADE_START_HOUR, TRADE_END_HOUR, SL_PIPS, TP_PIPS)

    while True:
        now = datetime.now(JST)

        # 取引時間外はスキップ
        if not (TRADE_START_HOUR <= now.hour < TRADE_END_HOUR):
            time.sleep(POLL_INTERVAL_SEC)
            continue

        state      = load_state()
        open_trade = state.get("open_trade")

        for pair in PAIRS:
            try:
                candles = client.get_candles(pair, "H1", 100)
                if not candles:
                    continue

                ind     = calc_indicators(candles)
                current = candles[-1].close
                high    = candles[-1].high
                low     = candles[-1].low

                if open_trade and open_trade["pair"] == pair:
                    # --- TP/SL チェック ---
                    result = _check_exit(open_trade, high, low)
                    if result:
                        closed = _close_trade(state, open_trade, result, current, now)
                        save_state({"open_trade": None})
                        _run_check(closed)
                        continue

                    # --- 逆シグナル決済 ---
                    rev_cond = _detect_condition(ind)
                    if rev_cond:
                        rev_key, _ = rev_cond
                        rev_dir = CONDITION_DIRECTION.get(rev_key)
                        if rev_dir and rev_dir != open_trade["direction"]:
                            logger.info("逆シグナル検出 (%s) → 早期決済", rev_key)
                            closed = _close_trade(state, open_trade, "SIGNAL_REVERSE", current, now)
                            save_state({"open_trade": None})
                            _run_check(closed)

                elif open_trade is None:
                    # --- エントリー判断 ---
                    condition = _detect_condition(ind)
                    if not condition:
                        continue

                    key, _ = condition
                    direction = CONDITION_DIRECTION.get(key)
                    if not direction:
                        continue

                    plan = _get_plan(pair)
                    allowed, reason = _plan_allows_entry(plan, direction, now)
                    if not allowed:
                        logger.debug("%s: エントリースキップ (%s)", pair, reason)
                        continue

                    sl_pips, tp_pips = _get_sl_tp_pips(plan, direction)
                    sl, tp = calc_sl_tp(pair, direction, current, sl_pips, tp_pips)

                    trade = {
                        "pair":        pair,
                        "direction":   direction,
                        "entry_price": current,
                        "entry_time":  now.isoformat(),
                        "sl_price":    round(sl, 5),
                        "tp_price":    round(tp, 5),
                        "condition":   key,
                        "plan_bias":   plan.get("bias", "NEUTRAL"),
                    }
                    state["open_trade"] = trade
                    save_state(state)

                    pair_label = pair.replace("_", "/")
                    logger.info("エントリー: %s %s @ %.5f (SL=%.5f / TP=%.5f) [Plan:%s]",
                                pair_label, direction, current, sl, tp, plan.get("bias", "NEUTRAL"))

            except Exception as e:
                logger.error("%s: エラー %s", pair, e)

        time.sleep(POLL_INTERVAL_SEC)


# ------------------------------------------------------------------
# 決済
# ------------------------------------------------------------------

def _check_exit(trade: dict, high: float, low: float) -> str | None:
    direction = trade["direction"]
    sl = trade["sl_price"]
    tp = trade["tp_price"]

    if direction == "BUY":
        if low  <= sl: return "SL"
        if high >= tp: return "TP"
    else:
        if high >= sl: return "SL"
        if low  <= tp: return "TP"
    return None


def _close_trade(state: dict, trade: dict, result: str, current: float, now: datetime) -> dict:
    if result in ("TP", "SL"):
        exit_price = trade["tp_price"] if result == "TP" else trade["sl_price"]
    else:
        exit_price = current  # SIGNAL_REVERSE は現在値で決済

    pip     = pip_value(trade["pair"])
    direction = trade["direction"]
    pips    = round((exit_price - trade["entry_price"]) / pip) if direction == "BUY" \
              else round((trade["entry_price"] - exit_price) / pip)

    closed = {**trade, "exit_price": exit_price, "exit_time": now.isoformat(),
              "result": result, "pips": pips}
    save_result(closed)

    pair_label = trade["pair"].replace("_", "/")
    logger.info("決済: %s %s → %s %+dpips @ %.5f",
                pair_label, direction, result, pips, exit_price)
    return closed


def _run_check(trade: dict) -> None:
    """決済後にCheckフェーズを自動実行"""
    try:
        from src.ai.checker import run_check
        result = run_check(trade)
        if result:
            logger.info("Check完了: bias_correct=%s | %s", result.bias_correct, result.cause)
    except Exception as e:
        logger.error("Check実行失敗: %s", e)


if __name__ == "__main__":
    run()
