"""Checkフェーズ: トレード結果をAIで評価し check_log.json に保存する"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

CHECK_LOG_FILE    = Path("data/check_log.json")
PLAN_HISTORY_FILE = Path("data/plan_history.json")
SIM_RESULTS_FILE  = Path("data/sim_results.json")

_SYSTEM_PROMPT = """\
あなたはプロのFXトレーダーです。取引結果とその時点の戦略方針を照合し、分析をJSONで出力してください。

## 厳守事項
- 必ず以下のJSONのみを返すこと。説明文・マークダウン・前置き・後書きは一切不要。
- コードブロック（```json）で囲むこと。

## 出力フォーマット
```json
{
  "bias_correct": true または false,
  "cause": "勝敗の主な原因（日本語100字程度）",
  "improvement": "次回のPlanへの改善提案（日本語100字程度）"
}
```

## 分析観点
- Planのbiasと取引方向・結果は一致していたか
- エントリー条件（テクニカル）は適切なタイミングだったか
- マクロ環境・経済指標の影響はあったか
- SL/TPの設定は相場のボラティリティに合っていたか
- 次回同じ状況でどう判断すべきか
"""


@dataclass
class CheckResult:
    trade_id:     str   # entry_time + pair で一意に識別
    pair:         str
    trade:        dict  # エントリー・決済情報
    plan:         dict  # 当時のPlan（なければ空）
    bias_correct: bool
    cause:        str
    improvement:  str
    timestamp:    str


# ------------------------------------------------------------------
# 状態管理
# ------------------------------------------------------------------

def load_check_log() -> list[dict]:
    if CHECK_LOG_FILE.exists():
        try:
            return json.loads(CHECK_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_check_log(result: CheckResult) -> None:
    log = load_check_log()
    log.append(asdict(result))
    CHECK_LOG_FILE.parent.mkdir(exist_ok=True)
    CHECK_LOG_FILE.write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _checked_ids() -> set[str]:
    return {entry["trade_id"] for entry in load_check_log()}


def _make_trade_id(trade: dict) -> str:
    return f"{trade.get('pair', '')}_{trade.get('entry_time', '')}"


# ------------------------------------------------------------------
# Plan照合
# ------------------------------------------------------------------

def _find_plan_at(pair: str, entry_time_str: str) -> dict:
    """エントリー時刻より前で最も直近のPlanを返す"""
    if not PLAN_HISTORY_FILE.exists():
        return {}
    try:
        history = json.loads(PLAN_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

    entry_dt = datetime.fromisoformat(entry_time_str)
    candidates = [
        p for p in history
        if p.get("pair") == pair
        and datetime.fromisoformat(p["timestamp"]) <= entry_dt
    ]
    if not candidates:
        return {}
    return max(candidates, key=lambda p: p["timestamp"])


# ------------------------------------------------------------------
# API呼び出し & パース
# ------------------------------------------------------------------

def _call_claude_check(user_prompt: str) -> str:
    import anthropic
    from src.config import ANTHROPIC_API_KEY, PRIMARY_MODEL
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=PRIMARY_MODEL,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text


def _build_check_prompt(trade: dict, plan: dict) -> str:
    context = {
        "trade": {
            "pair":        trade.get("pair"),
            "direction":   trade.get("direction"),
            "condition":   trade.get("condition"),
            "entry_price": trade.get("entry_price"),
            "entry_time":  trade.get("entry_time"),
            "exit_price":  trade.get("exit_price"),
            "exit_time":   trade.get("exit_time"),
            "result":      trade.get("result"),
            "pips":        trade.get("pips"),
        },
        "plan_at_entry": {
            "session":     plan.get("session", "不明"),
            "bias":        plan.get("bias", "不明"),
            "fundamental": plan.get("fundamental", ""),
            "technical":   plan.get("technical", ""),
            "plans":       plan.get("plans", []),
        } if plan else "Planデータなし（Plan実装前のトレード）",
    }
    return (
        "以下のトレード結果とその時点の戦略方針を分析してください。\n\n"
        f"```json\n{json.dumps(context, ensure_ascii=False, indent=2)}\n```"
    )


def _parse_check(raw: str) -> tuple[bool, str, str]:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    json_str = match.group(1) if match else raw.strip()
    if not match:
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            json_str = brace.group(0)

    try:
        data = json.loads(json_str)
        return (
            bool(data.get("bias_correct", False)),
            data.get("cause", ""),
            data.get("improvement", ""),
        )
    except Exception:
        logger.warning("Checkレスポンス解析失敗: %s", raw[:200])
        return False, "解析失敗", "解析失敗"


# ------------------------------------------------------------------
# メイン
# ------------------------------------------------------------------

def run_check(trade: dict) -> CheckResult | None:
    """1件のトレードを評価する。既チェック済みならNoneを返す"""
    trade_id = _make_trade_id(trade)

    if trade_id in _checked_ids():
        logger.debug("既チェック済みのためスキップ: %s", trade_id)
        return None

    pair       = trade.get("pair", "")
    entry_time = trade.get("entry_time", "")
    plan       = _find_plan_at(pair, entry_time)

    user_prompt = _build_check_prompt(trade, plan)

    try:
        raw = _call_claude_check(user_prompt)
        bias_correct, cause, improvement = _parse_check(raw)
    except Exception as e:
        logger.error("Check API呼び出し失敗: %s", e)
        bias_correct, cause, improvement = False, f"API失敗: {e}", ""

    result = CheckResult(
        trade_id=trade_id,
        pair=pair,
        trade=trade,
        plan=plan,
        bias_correct=bias_correct,
        cause=cause,
        improvement=improvement,
        timestamp=datetime.now(JST).isoformat(),
    )
    _save_check_log(result)
    logger.info("Check保存: %s result=%s bias_correct=%s", trade_id, trade.get("result"), bias_correct)
    return result


def run_check_all() -> list[CheckResult]:
    """sim_results.json の未チェックトレードを全件評価する"""
    if not SIM_RESULTS_FILE.exists():
        logger.info("sim_results.json が存在しません")
        return []

    try:
        trades = json.loads(SIM_RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("sim_results.json 読み込み失敗: %s", e)
        return []

    results = []
    for trade in trades:
        result = run_check(trade)
        if result:
            results.append(result)

    return results
