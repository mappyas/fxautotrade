"""
Alpha Vantage FX データクライアント

無料プラン制限:
  - 5 リクエスト / 分
  - 25 リクエスト / 日

2年分の M5 データ = 24 ヶ月 = 24 リクエスト / ペア
1ペアなら1日で取り切れる。2ペア同時は2日かかる場合がある。
月ごとにキャッシュするため、途中で止まっても再実行で続きから取得できる。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.data.oanda_client import Candle

logger = logging.getLogger(__name__)

_BASE_URL   = "https://www.alphavantage.co/query"
_REQ_DELAY  = 13   # 秒（無料枠 5req/min = 12秒間隔 + バッファ）

_PAIR_SYMBOLS: dict[str, tuple[str, str]] = {
    "USD_JPY": ("USD", "JPY"),
    "EUR_USD": ("EUR", "USD"),
    "GBP_USD": ("GBP", "USD"),
    "EUR_JPY": ("EUR", "JPY"),
    "AUD_USD": ("AUD", "USD"),
    "GBP_JPY": ("GBP", "JPY"),
}

_INTERVAL_MAP: dict[str, str] = {
    "M5":  "5min",
    "M15": "15min",
    "M30": "30min",
    "H1":  "60min",
}


def fetch_range(
    pair: str,
    tf: str,
    months_back: int,
    api_key: str,
    cache_dir: Path,
) -> list[Candle]:
    """
    指定した期間の全ローソク足を取得する。
    月ごとにキャッシュするため、中断・再開に対応。

    Args:
        pair       : "EUR_USD" 形式
        tf         : "M5" | "M15" | "H1" など
        months_back: 今月から何ヶ月前まで取得するか
        api_key    : Alpha Vantage API キー
        cache_dir  : キャッシュ保存先ディレクトリ

    Returns:
        Candle のリスト（古い順）
    """
    syms = _PAIR_SYMBOLS.get(pair)
    if syms is None:
        raise ValueError(f"未対応ペア: {pair}")

    interval = _INTERVAL_MAP.get(tf)
    if interval is None:
        raise ValueError(f"未対応足: {tf}")

    cache_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    months: list[str] = []
    for i in range(months_back - 1, -1, -1):
        year  = now.year  - (now.month - 1 - (months_back - 1 - i)) // 12
        month = (now.month - 1 - (months_back - 1 - i)) % 12 + 1
        # 月計算を正しく
        total_months = now.year * 12 + now.month - 1 - i
        year  = total_months // 12
        month = total_months % 12 + 1
        months.append(f"{year:04d}-{month:02d}")

    all_candles: list[Candle] = []
    fetched = 0

    for ym in months:
        cache_file = cache_dir / f"{pair}_{tf}_av_{ym}.json"

        if cache_file.exists():
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            candles = _raw_to_candles(raw)
            all_candles.extend(candles)
            logger.debug("キャッシュ読込: %s (%d本)", cache_file.name, len(candles))
            continue

        # API 取得
        logger.info("AV 取得中: %s %s %s ...", pair, tf, ym)
        try:
            candles = _fetch_month(syms[0], syms[1], interval, ym, api_key)
        except RateLimitError:
            logger.error(
                "レート制限に達しました（25リクエスト/日）。"
                "明日再実行してください。%d ヶ月分取得済み。", fetched
            )
            break
        except Exception as e:
            logger.error("取得失敗 %s %s: %s", pair, ym, e)
            break

        cache_file.write_text(
            json.dumps(
                [{"t": c.time.isoformat(), "o": c.open, "h": c.high,
                  "l": c.low, "c": c.close}
                 for c in candles],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info("  → %d 本取得・保存", len(candles))
        all_candles.extend(candles)
        fetched += 1

        time.sleep(_REQ_DELAY)

    # 時刻順ソート・重複除去
    seen: set[str] = set()
    unique: list[Candle] = []
    for c in sorted(all_candles, key=lambda x: x.time):
        key = c.time.isoformat()
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique


# ------------------------------------------------------------------
# 内部ヘルパー
# ------------------------------------------------------------------

class RateLimitError(Exception):
    pass


def _fetch_month(
    from_sym: str,
    to_sym: str,
    interval: str,
    month: str,
    api_key: str,
) -> list[Candle]:
    params = {
        "function":    "FX_INTRADAY",
        "from_symbol": from_sym,
        "to_symbol":   to_sym,
        "interval":    interval,
        "month":       month,
        "outputsize":  "full",
        "apikey":      api_key,
        "datatype":    "csv",
    }

    resp = requests.get(_BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    text = resp.text

    # エラー判定
    if "Note" in text and "API call frequency" in text:
        raise RateLimitError("1分あたりのリクエスト上限")
    if "Information" in text and "25 requests per day" in text:
        raise RateLimitError("1日あたりのリクエスト上限（25件）")
    if "{" in text[:50]:
        # JSON が返ってきた = エラーレスポンス
        try:
            err = json.loads(text)
            msg = err.get("Information") or err.get("Note") or err.get("Error Message", text[:200])
            if "25 requests" in msg or "call frequency" in msg:
                raise RateLimitError(msg)
            raise RuntimeError(f"AV APIエラー: {msg}")
        except (json.JSONDecodeError, RateLimitError):
            raise
        except RuntimeError:
            raise

    return _parse_csv(text)


def _parse_csv(text: str) -> list[Candle]:
    candles: list[Candle] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            t = datetime.strptime(
                row["timestamp"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            candles.append(Candle(
                time=t,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=0,
            ))
        except Exception:
            continue
    return candles


def _raw_to_candles(raw: list[dict]) -> list[Candle]:
    return [
        Candle(
            time=datetime.fromisoformat(r["t"]),
            open=r["o"], high=r["h"], low=r["l"], close=r["c"],
            volume=0,
        )
        for r in raw
    ]
