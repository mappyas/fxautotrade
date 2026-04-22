"""
FX AutoBuy ダッシュボード
Streamlit + Plotly による可視化UI
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

from src.ai.analyzer import analyze
from src.ai.indicators import calc_indicators
from src.config import CANDLE_COUNTS, DISCORD_WEBHOOK_URL, FINNHUB_API_KEY, PAIRS, PAPER_TRADE, SCALP_CANDLE_COUNTS
from src.notifications.alert_filter import check_and_notify
from src.data.client_factory import get_data_client
from src.data.economic_calendar import fetch_economic_events
from src.trading.order import execute
from src.trading.session import get_session

LOG_FILE = Path("data/signal_log.json")
JST = timezone(timedelta(hours=9))


# ------------------------------------------------------------------
# パスワード保護
# ------------------------------------------------------------------
def _check_auth() -> None:
    """st.secrets のパスワードと照合。未認証なら入力フォームを表示して停止。"""
    correct = st.secrets.get("PASSWORD", "")
    if not correct:
        st.error("secrets に PASSWORD が設定されていません")
        st.stop()

    if st.session_state.get("authenticated"):
        return

    st.markdown("## FX AutoBuy — ログイン")
    pw = st.text_input("パスワード", type="password")
    if st.button("ログイン"):
        if pw == correct:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    st.stop()


def now_jst() -> datetime:
    return datetime.now(JST)


def to_jst(dt: datetime) -> datetime:
    """UTC or aware datetime を JST に変換"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST)


# ------------------------------------------------------------------
# ページ設定
# ------------------------------------------------------------------
st.set_page_config(
    page_title="FX AutoBuy Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="metric-container"] { background: #1e1e2e; border-radius: 8px; padding: 12px; }
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------
# ログ管理
# ------------------------------------------------------------------
def load_log() -> list[dict]:
    if LOG_FILE.exists():
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def append_log(entry: dict) -> None:
    entries = load_log()
    entries.append(entry)
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries[-500:], f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
# SL/TP 価格計算（order.py と同じロジック）
# ------------------------------------------------------------------
def calc_sl_tp_prices(
    action: str,
    current_price: float,
    sl_pips: int,
    tp_pips: int,
    pair: str,
) -> tuple[float, float]:
    pip = 0.01 if pair.endswith("JPY") else 0.0001
    sl_delta = sl_pips * pip
    tp_delta = tp_pips * pip
    if action == "BUY":
        return round(current_price - sl_delta, 5), round(current_price + tp_delta, 5)
    else:
        return round(current_price + sl_delta, 5), round(current_price - tp_delta, 5)


# ------------------------------------------------------------------
# チャート描画
# ------------------------------------------------------------------
def _rolling_sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < period:
            result.append(None)
        else:
            result.append(sum(values[i + 1 - period : i + 1]) / period)
    return result


def build_chart(candles, pair: str, signal=None) -> go.Figure:
    # キャンドル時刻を JST に変換
    times  = [to_jst(c.time) for c in candles]
    opens  = [c.open  for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]
    closes = [c.close for c in candles]

    sma20 = _rolling_sma(closes, 20)
    sma50 = _rolling_sma(closes, 50)

    # RSI計算
    rsi_vals: list[float | None] = [None] * len(closes)
    period = 14
    if len(closes) > period:
        diffs  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [d if d > 0 else 0.0 for d in diffs]
        losses = [-d if d < 0 else 0.0 for d in diffs]
        for i in range(period, len(closes)):
            avg_g = sum(gains[i - period : i]) / period
            avg_l = sum(losses[i - period : i]) / period
            if avg_l == 0:
                rsi_vals[i] = 100.0
            else:
                rs = avg_g / avg_l
                rsi_vals[i] = 100 - (100 / (1 + rs))

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.72, 0.28],
        vertical_spacing=0.04,
        subplot_titles=(pair.replace("_", "/") + " H1 (JST)", "RSI(14)"),
    )

    # ローソク足
    fig.add_trace(go.Candlestick(
        x=times, open=opens, high=highs, low=lows, close=closes,
        name="H1",
        increasing_line_color="#26A69A",
        decreasing_line_color="#EF5350",
        increasing_fillcolor="#26A69A",
        decreasing_fillcolor="#EF5350",
    ), row=1, col=1)

    # SMA20
    fig.add_trace(go.Scatter(
        x=times, y=sma20,
        name="SMA20", line=dict(color="#FFA726", width=1.5),
        connectgaps=False,
    ), row=1, col=1)

    # SMA50
    fig.add_trace(go.Scatter(
        x=times, y=sma50,
        name="SMA50", line=dict(color="#42A5F5", width=1.5),
        connectgaps=False,
    ), row=1, col=1)

    # SL / TP ライン（シグナルが BUY or SELL のとき）
    if signal and signal.action in ("BUY", "SELL") and closes:
        sl_price, tp_price = calc_sl_tp_prices(
            signal.action, closes[-1],
            signal.suggested_sl_pips, signal.suggested_tp_pips,
            pair,
        )
        fig.add_hline(
            y=sl_price,
            line=dict(color="#EF5350", dash="dash", width=1.5),
            annotation_text=f"SL {sl_price:.3f}",
            annotation_font_color="#EF5350",
            annotation_position="right",
            row=1, col=1,
        )
        fig.add_hline(
            y=tp_price,
            line=dict(color="#26A69A", dash="dash", width=1.5),
            annotation_text=f"TP {tp_price:.3f}",
            annotation_font_color="#26A69A",
            annotation_position="right",
            row=1, col=1,
        )
        # エントリー価格（現在値）
        fig.add_hline(
            y=closes[-1],
            line=dict(color="#FFA726", dash="dot", width=1),
            annotation_text=f"Entry {closes[-1]:.3f}",
            annotation_font_color="#FFA726",
            annotation_position="right",
            row=1, col=1,
        )

    # RSI
    fig.add_trace(go.Scatter(
        x=times, y=rsi_vals,
        name="RSI14", line=dict(color="#AB47BC", width=1.5),
        connectgaps=False,
    ), row=2, col=1)
    fig.add_hrect(y0=70, y1=100, fillcolor="red",   opacity=0.06, row=2, col=1, line_width=0)
    fig.add_hrect(y0=0,  y1=30,  fillcolor="green", opacity=0.06, row=2, col=1, line_width=0)
    fig.add_hline(y=70, line_color="red",   line_dash="dot", line_width=1, row=2, col=1)
    fig.add_hline(y=30, line_color="green", line_dash="dot", line_width=1, row=2, col=1)

    fig.update_layout(
        height=480,
        xaxis_rangeslider_visible=False,
        plot_bgcolor="#0e1117",
        paper_bgcolor="#0e1117",
        font_color="#c9d1d9",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    fig.update_xaxes(gridcolor="#21262d", showgrid=True)
    fig.update_yaxes(gridcolor="#21262d", showgrid=True)

    return fig


# ------------------------------------------------------------------
# シグナルカード
# ------------------------------------------------------------------
def render_signal_card(signal, current_price: float, pair: str) -> None:
    color_map = {"BUY": "#26A69A", "SELL": "#EF5350", "HOLD": "#888888"}
    icon_map  = {"BUY": "▲", "SELL": "▼", "HOLD": "━"}
    color = color_map.get(signal.action, "#888888")
    icon  = icon_map.get(signal.action, "━")

    sl_tp_html = ""
    if signal.action in ("BUY", "SELL"):
        sl_price, tp_price = calc_sl_tp_prices(
            signal.action, current_price,
            signal.suggested_sl_pips, signal.suggested_tp_pips,
            pair,
        )
        sl_tp_html = f"""
  <div style="display:flex; gap:12px; margin:8px 0;">
    <div style="flex:1; background:#EF535022; border:1px solid #EF5350;
                border-radius:6px; padding:8px; text-align:center;">
      <div style="color:#EF5350; font-size:0.7em; font-weight:bold;">STOP LOSS</div>
      <div style="color:#EF5350; font-size:1.1em; font-weight:bold;">{sl_price:.3f}</div>
      <div style="color:#888; font-size:0.7em;">{signal.suggested_sl_pips}pips</div>
    </div>
    <div style="flex:1; background:#26A69A22; border:1px solid #26A69A;
                border-radius:6px; padding:8px; text-align:center;">
      <div style="color:#26A69A; font-size:0.7em; font-weight:bold;">TAKE PROFIT</div>
      <div style="color:#26A69A; font-size:1.1em; font-weight:bold;">{tp_price:.3f}</div>
      <div style="color:#888; font-size:0.7em;">{signal.suggested_tp_pips}pips</div>
    </div>
  </div>"""

    st.markdown(f"""
<div style="
    background:{color}18; border:2px solid {color};
    border-radius:10px; padding:16px; margin:4px 0;
">
  <div style="display:flex; align-items:center; gap:10px;">
    <span style="font-size:2em; color:{color};">{icon}</span>
    <span style="font-size:1.8em; font-weight:bold; color:{color};">{signal.action}</span>
    <span style="font-size:1.1em; color:#aaa; margin-left:auto;">
      {signal.confidence:.0%} confidence
    </span>
  </div>
  <hr style="border-color:{color}44; margin:8px 0;">
  <p style="font-size:0.85em; color:#aaa; margin:4px 0;">{signal.timeframe}</p>
  {sl_tp_html}
  <p style="font-size:0.9em; margin:8px 0; line-height:1.5;">{signal.reasoning}</p>
  <p style="font-size:0.75em; color:#666; margin:0;">
    model: {signal.model_used}{"&nbsp;(fallback)" if signal.fallback_used else ""}
  </p>
</div>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------
# 分析実行
# ------------------------------------------------------------------
def run_analysis(client, pairs: list[str], trade_mode: str = "daytrading") -> dict:
    results = {}
    progress = st.progress(0, text="分析準備中...")

    # 経済指標を一度だけ取得（全ペア共通）
    economic_events = fetch_economic_events(FINNHUB_API_KEY)

    candle_counts = SCALP_CANDLE_COUNTS if trade_mode == "scalping" else CANDLE_COUNTS

    for idx, pair in enumerate(pairs):
        progress.progress(idx / len(pairs), text=f"{pair} 分析中...")
        try:
            candles   = client.get_multi_granularity_candles(pair, candle_counts)
            positions = client.get_open_positions()

            # スキャル: M15がメイン足、デイトレ: H1がメイン足
            main_tf = "M15" if trade_mode == "scalping" else "H1"
            mid_tf  = "M30" if trade_mode == "scalping" else "H4"
            long_tf = "H1"  if trade_mode == "scalping" else "D"

            if not candles.get(main_tf):
                st.warning(f"{pair}: ローソク足データなし")
                continue

            signal = analyze(
                pair=pair,
                candles_h1=candles.get(main_tf, []),
                candles_h4=candles.get(mid_tf, []),
                candles_d=candles.get(long_tf, []),
                open_positions=positions,
                economic_events=economic_events,
                trade_mode=trade_mode,
            )

            session = get_session(pair)
            result = execute(client, signal, pair, daily_pnl=0.0)
            results[pair] = {"candles": candles, "signal": signal, "result": result, "session": session}

            current_price = candles["H1"][-1].close if candles.get("H1") else 0.0
            if signal.action in ("BUY", "SELL"):
                sl_price, tp_price = calc_sl_tp_prices(
                    signal.action, current_price,
                    signal.suggested_sl_pips, signal.suggested_tp_pips,
                    pair,
                )
            else:
                sl_price, tp_price = None, None

            append_log({
                "timestamp":   now_jst().strftime("%Y-%m-%d %H:%M JST"),
                "pair":        pair,
                "action":      signal.action,
                "confidence":  round(signal.confidence, 4),
                "entry":       round(current_price, 5),
                "sl":          round(sl_price, 5) if sl_price else None,
                "tp":          round(tp_price, 5) if tp_price else None,
                "sl_pips":     signal.suggested_sl_pips,
                "tp_pips":     signal.suggested_tp_pips,
                "session":     session.name,
                "recommended": session.recommended,
                "caution":     session.caution,
                "close_by":    f"{getattr(session, 'close_by_hour', None):02d}:00 JST" if getattr(session, "close_by_hour", None) is not None else "—",
                "session_note": session.reason,
                "reasoning":   signal.reasoning,
                "model":       signal.model_used,
                "fallback":    signal.fallback_used,
                "executed":    result.executed,
                "paper":       result.paper,
                "skip_reason": result.reason if not result.executed else "",
            })
        except Exception as e:
            st.error(f"{pair}: {e}")

    progress.progress(1.0, text="完了!")
    return results


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_candles(p: str) -> list:
    return get_data_client().get_candles(p, "H1", 48)


# ------------------------------------------------------------------
# メイン
# ------------------------------------------------------------------
def main() -> None:
    _check_auth()

    # ---- 自動更新（チャートのみ、AI分析なし）----
    # streamlit-autorefresh が入っていれば使う、なければスキップ
    chart_refresh_sec: int | None = None
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
        _autorefresh_available = True
    except ImportError:
        _autorefresh_available = False

    # ---- サイドバー ----
    with st.sidebar:
        st.title("FX AutoBuy")
        st.caption("Powered by Groq + yfinance")
        st.divider()

        mode_color = "orange" if PAPER_TRADE else "red"
        mode_text  = "PAPER TRADE" if PAPER_TRADE else "LIVE TRADE"
        st.markdown(
            f'<div style="text-align:center; padding:8px; background:#21262d; '
            f'border-radius:6px; color:{mode_color}; font-weight:bold;">'
            f'{mode_text}</div>',
            unsafe_allow_html=True,
        )
        st.divider()

        trade_mode = st.radio(
            "トレードモード",
            ["daytrading", "scalping"],
            format_func=lambda x: "デイトレ（H1/H4/D）" if x == "daytrading" else "スキャル（M15/M30/H1）",
            horizontal=True,
        )

        selected_pairs = st.multiselect("通貨ペア", PAIRS, default=PAIRS)

        run_btn = st.button(
            "AI分析を実行",
            use_container_width=True,
            type="primary",
            disabled=not selected_pairs,
        )

        st.divider()
        st.caption("チャート自動更新（価格のみ）")
        refresh_options = {"OFF": 0, "1分": 60, "5分": 300, "15分": 900}
        refresh_label = st.selectbox("更新間隔", list(refresh_options.keys()), index=0)
        chart_refresh_sec = refresh_options[refresh_label]

        if chart_refresh_sec and _autorefresh_available:
            st_autorefresh(interval=chart_refresh_sec * 1000, key="chart_refresh")
            st.caption(f"{refresh_label}ごとに価格を更新中")
        elif chart_refresh_sec and not _autorefresh_available:
            st.warning("`pip install streamlit-autorefresh` で自動更新が有効になります")

    # ---- ヘッダー ----
    st.markdown("## FX AutoBuy Dashboard")
    st.caption(f"現在時刻: {now_jst().strftime('%Y-%m-%d %H:%M JST')}")

    # ---- テクニカルアラートチェック（AIなし・毎回実行）----
    if DISCORD_WEBHOOK_URL:
        alerted = []
        for pair in selected_pairs:
            candles = _fetch_candles(pair)
            if candles:
                ind = calc_indicators(candles)
                fired = check_and_notify(pair, ind, DISCORD_WEBHOOK_URL)
                if fired:
                    alerted.append(f"{pair.replace('_', '/')} ({fired})")
        if alerted:
            st.toast(f"Discord通知送信: {', '.join(alerted)}", icon="🔔")

    # ---- クライアント & 口座情報 ----
    client  = get_data_client()
    account = client.get_account_summary()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("残高",             f"¥{account.balance:,.0f}")
    c2.metric("NAV",              f"¥{account.nav:,.0f}")
    c3.metric("含み損益",
              f"¥{account.unrealized_pnl:+,.0f}",
              delta=f"{account.unrealized_pnl:+,.0f}")
    c4.metric("オープンポジション", len(client.get_open_positions()))

    st.divider()

    # ---- 経済指標カレンダー ----
    with st.expander("経済指標カレンダー（本日〜明日）", expanded=False):
        events = fetch_economic_events(FINNHUB_API_KEY)
        if events:
            impact_color = {"high": "🔴", "medium": "🟡", "low": "⚪"}
            df_ev = pd.DataFrame(events)
            df_ev["impact"] = df_ev["impact"].map(lambda x: f"{impact_color.get(x, '')} {x.upper()}")
            df_ev = df_ev.rename(columns={
                "time": "日時", "country": "国", "impact": "影響度",
                "event": "イベント", "forecast": "予想", "actual": "結果", "prev": "前回",
            })
            st.dataframe(df_ev[["日時", "国", "影響度", "イベント", "予想", "結果", "前回"]],
                         use_container_width=True, hide_index=True)
        else:
            st.caption("該当する経済指標はありません（または API キー未設定）")

    st.divider()

    # ---- 分析実行 ----
    if run_btn:
        with st.spinner(""):
            results = run_analysis(client, selected_pairs, trade_mode)
        st.session_state["last_results"] = results
        st.session_state["last_run"] = now_jst().strftime("%Y-%m-%d %H:%M JST")
        st.success(f"分析完了 — {st.session_state['last_run']}")

    results: dict = st.session_state.get("last_results", {})

    if st.session_state.get("last_run"):
        st.caption(f"最終AI分析: {st.session_state['last_run']}")

    # ---- ペアタブ ----
    if selected_pairs:
        tabs = st.tabs([p.replace("_", "/") for p in selected_pairs])
        for tab, pair in zip(tabs, selected_pairs):
            with tab:
                pair_result = results.get(pair)

                h1 = (
                    pair_result["candles"].get("H1", [])
                    if pair_result
                    else _fetch_candles(pair)
                )

                signal = pair_result["signal"] if pair_result else None

                col_chart, col_signal = st.columns([2, 1], gap="medium")

                with col_chart:
                    if h1:
                        st.plotly_chart(
                            build_chart(h1, pair, signal=signal),
                            use_container_width=True,
                        )

                        ind = calc_indicators(h1)
                        i1, i2, i3, i4, i5 = st.columns(5)
                        current = h1[-1].close
                        prev    = h1[-2].close if len(h1) > 1 else current
                        i1.metric("現在値",  f"{current:.3f}", f"{current - prev:+.3f}")
                        i2.metric("SMA20",  f"{ind.sma20:.3f}"  if ind.sma20  else "—")
                        i3.metric("SMA50",  f"{ind.sma50:.3f}"  if ind.sma50  else "—")
                        i4.metric("RSI14",  f"{ind.rsi14:.1f}"  if ind.rsi14  else "—")
                        i5.metric("トレンド", ind.trend)
                    else:
                        st.warning("ローソク足データを取得できませんでした")

                with col_signal:
                    st.subheader("最新シグナル")

                    # セッション表示（常時）
                    cur_session = get_session(pair)
                    close_by_hour = getattr(cur_session, "close_by_hour", None)
                    close_by_str = (
                        f"　→ **{close_by_hour:02d}:00 JST までに決済推奨**"
                        if close_by_hour is not None else ""
                    )
                    if cur_session.recommended:
                        st.success(f"◎ {cur_session.name}セッション{close_by_str}")
                    elif cur_session.caution:
                        st.warning(f"△ {cur_session.name}セッション — {cur_session.reason}")
                    else:
                        st.error(f"✕ {cur_session.name}セッション — {cur_session.reason}")

                    if pair_result:
                        signal = pair_result["signal"]
                        result = pair_result["result"]
                        current_price = h1[-1].close if h1 else 0.0
                        render_signal_card(signal, current_price, pair)
                        if result.executed:
                            label = "PAPER 実行済" if result.paper else "注文 実行済"
                            st.success(label)
                        else:
                            st.info(f"スキップ: {result.reason}")
                    else:
                        st.info("「AI分析を実行」を押してください")

                    # オープンポジション
                    positions = [p for p in client.get_open_positions() if p.instrument == pair]
                    if positions:
                        st.subheader("ポジション")
                        for pos in positions:
                            icon = "🟢" if pos.direction == "LONG" else "🔴"
                            open_jst = to_jst(pos.open_time).strftime("%m/%d %H:%M JST")
                            st.markdown(
                                f"{icon} **{pos.direction}** {pos.units}u "
                                f"@ {pos.open_price:.3f}"
                            )
                            st.caption(f"PnL: {pos.unrealized_pnl:+.0f} | {open_jst}")

    # ---- シグナル履歴 ----
    st.divider()
    st.subheader("シグナル履歴")

    log = load_log()
    if log:
        df = pd.DataFrame(list(reversed(log[-100:])))

        display_cols = ["timestamp", "pair", "action", "confidence", "entry", "sl", "tp", "sl_pips", "tp_pips", "session", "recommended", "close_by", "reasoning", "executed", "paper"]
        existing = [c for c in display_cols if c in df.columns]
        df = df[existing].copy()
        df["confidence"] = df["confidence"].apply(lambda x: f"{float(x):.0%}")
        if "recommended" in df.columns and "caution" in df.columns:
            def _rec_label(row):
                if row.get("recommended"):
                    return "◎ 推奨"
                elif row.get("caution"):
                    return "△ 注意"
                else:
                    return "✕ 非推奨"
            df["recommended"] = df.apply(_rec_label, axis=1)
            df = df.drop(columns=["caution"], errors="ignore")
        df = df.rename(columns={
            "timestamp":    "時刻 (JST)",
            "pair":         "ペア",
            "action":       "シグナル",
            "confidence":   "信頼度",
            "entry":        "エントリー",
            "sl":           "SL価格",
            "tp":           "TP価格",
            "sl_pips":      "SL(pips)",
            "tp_pips":      "TP(pips)",
            "session":     "セッション",
            "recommended": "推奨",
            "close_by":    "決済推奨期限",
            "reasoning":    "判断理由",
            "executed":     "実行",
            "paper":        "PAPER",
        })
        st.dataframe(df, use_container_width=True, hide_index=True)

        if st.button("ログをクリア", type="secondary"):
            LOG_FILE.unlink(missing_ok=True)
            st.rerun()
    else:
        st.info("まだシグナル履歴がありません。分析を実行してください。")


if __name__ == "__main__":
    main()
