"""
dashboard/app.py
EUR/USD Forex Bot — Streamlit Control Center
─────────────────────────────────────────────
4 tabs: Live Overview | Strategy Comparison | Trade Journal | Risk & Kill Switches
Auto-refreshes every 30 seconds.  Standalone — no MT5 required.

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys, os, io, math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
from sqlalchemy import text

import config
from core.database        import init_db, get_session
from core.session_manager import get_session_info, is_market_open
from core.news_filter     import NewsFilter
from analytics.trade_logger  import TradeLogger
from analytics.performance   import PerformanceAnalyzer

# ─────────────────────────────────────────────────────────────
# Page config  (must be first Streamlit call)
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title = "EUR/USD Forex Bot",
    page_icon  = "📈",
    layout     = "wide",
    initial_sidebar_state = "expanded",
)

# ─────────────────────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────────────────────
GREEN  = "#00C851"
RED    = "#FF4444"
GOLD   = "#FFD700"
ORANGE = "#FF8800"
GREY   = "#888888"
BG     = "#0E1117"
CARD   = "#1E2130"

# ─────────────────────────────────────────────────────────────
# Global CSS injection
# ─────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
  /* Base dark theme tweaks */
  html, body, [data-testid="stAppViewContainer"] {{
      background-color: {BG};
      color: #FAFAFA;
  }}
  [data-testid="stSidebar"] {{ background-color: #161B27; }}

  /* Metric card borders */
  [data-testid="metric-container"] {{
      background: {CARD};
      border: 1px solid #2D3348;
      border-radius: 8px;
      padding: 12px 16px;
  }}

  /* Paper-mode banner */
  .paper-banner {{
      background: #2B2D0F;
      border: 1px solid {GOLD};
      color: {GOLD};
      border-radius: 6px;
      padding: 8px 14px;
      font-weight: 600;
      margin-bottom: 10px;
  }}

  /* Kill-switch banner */
  .kill-banner {{
      background: #2B0A0A;
      border: 2px solid {RED};
      color: {RED};
      border-radius: 6px;
      padding: 10px 16px;
      font-weight: 700;
      font-size: 1.1rem;
      text-align: center;
      margin-bottom: 14px;
  }}

  /* Coloured result badges */
  .win  {{ color: {GREEN}; font-weight: 600; }}
  .loss {{ color: {RED};   font-weight: 600; }}
  .open {{ color: {GOLD};  font-weight: 600; }}

  /* Section card */
  .section-card {{
      background: {CARD};
      border: 1px solid #2D3348;
      border-radius: 10px;
      padding: 16px 20px;
      margin-bottom: 16px;
  }}

  /* Thin hr */
  hr {{ border-color: #2D3348; }}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# Shared initialisation (cached across reruns)
# ─────────────────────────────────────────────────────────────
@st.cache_resource
def _init():
    init_db()
    db      = get_session()
    logger  = TradeLogger(db_session=db)
    perf    = PerformanceAnalyzer(db_session=db)
    news    = NewsFilter()
    return db, logger, perf, news

DB, LOGGER, PERF, NEWS = _init()

# ─────────────────────────────────────────────────────────────
# Cached DB queries  (TTL = 30 s)
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def _comparison() -> list[dict]:
    return PERF.get_all_strategies_comparison()

@st.cache_data(ttl=30)
def _recent(limit: int = 50, strategy: Optional[str] = None) -> list[dict]:
    return PERF.get_recent_trades(limit=limit, strategy=strategy or None)

@st.cache_data(ttl=30)
def _open_trades() -> list[dict]:
    return LOGGER.get_open_db_trades()

@st.cache_data(ttl=30)
def _today_summary() -> dict:
    return PERF.get_daily_summary(date.today())

@st.cache_data(ttl=30)
def _all_trades(limit: int = 500) -> list[dict]:
    return LOGGER.get_all_trades(limit=limit)

@st.cache_data(ttl=30)
def _session_info() -> dict:
    return get_session_info()

@st.cache_data(ttl=60)
def _news_safe() -> bool:
    return NEWS.is_safe_to_trade()

@st.cache_data(ttl=60)
def _next_high_impact() -> Optional[dict]:
    return NEWS.get_next_high_impact()

# ─────────────────────────────────────────────────────────────
# Session-state defaults
# ─────────────────────────────────────────────────────────────
def _init_state():
    if "paper_mode"    not in st.session_state:
        st.session_state.paper_mode = False
    if "kill_active"   not in st.session_state:
        st.session_state.kill_active = False
    if "last_refresh"  not in st.session_state:
        st.session_state.last_refresh = datetime.now(timezone.utc)
    for name in config.STRATEGIES:
        key = f"toggle_{name}"
        if key not in st.session_state:
            st.session_state[key] = config.STRATEGIES[name]["enabled"]

_init_state()

# ─────────────────────────────────────────────────────────────
# Password gate (set DASHBOARD_PASSWORD env var to enable)
# ─────────────────────────────────────────────────────────────
_PWD = os.getenv("DASHBOARD_PASSWORD", "")
if _PWD and not st.session_state.get("authenticated"):
    st.markdown(
        "<h2 style='text-align:center;margin-top:80px'>🔒 EUR/USD Forex Bot</h2>",
        unsafe_allow_html=True,
    )
    with st.form("login_form"):
        entered = st.text_input("Password", type="password", placeholder="Enter dashboard password")
        submitted = st.form_submit_button("Unlock", use_container_width=True)
        if submitted:
            if entered == _PWD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    st.stop()

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _colour_pnl(val: float) -> str:
    if val > 0: return f"<span style='color:{GREEN}'>${val:+.2f}</span>"
    if val < 0: return f"<span style='color:{RED}'>${val:+.2f}</span>"
    return f"$0.00"

def _result_badge(result: str) -> str:
    cls = {"WIN": "win", "LOSS": "loss", "OPEN": "open"}.get(result, "")
    return f"<span class='{cls}'>{result}</span>"

def _pct_bar(value: float, limit: float) -> None:
    """Draw a coloured progress bar for risk meters."""
    pct = min(value / limit, 1.0) if limit > 0 else 0.0
    if pct < 0.5:   colour = GREEN
    elif pct < 0.75: colour = ORANGE
    else:            colour = RED
    st.markdown(f"""
    <div style="background:#2D3348;border-radius:6px;height:18px;width:100%;overflow:hidden;">
      <div style="background:{colour};height:100%;width:{pct*100:.1f}%;transition:width .4s;"></div>
    </div>
    <div style="font-size:0.75rem;color:{GREY};margin-top:3px;">
      {value:.2f}% of {limit:.1f}% limit ({pct*100:.0f}%)
    </div>
    """, unsafe_allow_html=True)

def _plotly_bar(df: pd.DataFrame, x: str, y: str, title: str,
                colors: list | None = None) -> go.Figure:
    fig = px.bar(
        df, x=x, y=y, title=title,
        color_discrete_sequence=colors or [GREEN],
        template="plotly_dark",
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=36, b=10),
        paper_bgcolor=CARD, plot_bgcolor=CARD,
        font=dict(size=11),
        title_font_size=13,
        showlegend=False,
    )
    fig.update_traces(marker_line_width=0)
    return fig

# ─────────────────────────────────────────────────────────────
# ░░ SIDEBAR ░░
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📈 Forex Bot")
    st.markdown("---")

    # Paper mode toggle
    st.session_state.paper_mode = st.toggle(
        "📄 Paper Mode",
        value=st.session_state.paper_mode,
        help="Strategies run but NO real MT5 orders are placed.",
    )
    if st.session_state.paper_mode:
        st.markdown(
            "<div class='paper-banner'>📄 PAPER MODE — no real orders</div>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # Bot status
    sess       = _session_info()
    is_open    = sess["market_open"]
    is_safe    = _news_safe()
    kill_in_db = DB.execute(
        text("SELECT kill_switch_triggered FROM daily_summary WHERE date=CURRENT_DATE")
    ).fetchone()
    kill_active = bool(kill_in_db and kill_in_db[0])

    if kill_active:
        status_label, status_colour = "🛑 HALTED", RED
    elif not is_open:
        status_label, status_colour = "🌙 MARKET CLOSED", GREY
    elif sess["session"] == "dead":
        status_label, status_colour = "💤 DEAD ZONE", GREY
    elif not is_safe:
        status_label, status_colour = "📰 NEWS PAUSE", ORANGE
    elif st.session_state.paper_mode:
        status_label, status_colour = "📄 PAPER MODE", GOLD
    else:
        status_label, status_colour = "🟢 RUNNING", GREEN

    st.markdown(
        f"<div style='font-size:1.1rem;font-weight:700;color:{status_colour}'>"
        f"{status_label}</div>",
        unsafe_allow_html=True,
    )

    # Last tick
    st.caption(f"Dashboard loaded: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

    st.markdown("---")

    # Quick stats
    stats_all = _comparison()
    total_trades_all = sum(s["total_trades"] for s in stats_all)
    total_wins_all   = sum(s["wins"]         for s in stats_all)
    total_pnl_all    = sum(s["total_pnl_usd"] for s in stats_all)
    wr_all = round(total_wins_all / total_trades_all * 100, 1) if total_trades_all else 0.0

    st.markdown("**All-time Stats**")
    st.metric("Total Trades",   total_trades_all)
    st.metric("Overall Win Rate", f"{wr_all}%")
    st.metric("All-time PnL",   f"${total_pnl_all:+.2f}")

    st.markdown("---")

    # Manual balance input (dashboard standalone)
    st.markdown("**Account**")
    account_balance = st.number_input(
        "Balance (USD)", min_value=0.0, value=10_000.0, step=100.0, format="%.2f"
    )

    # Auto-refresh note
    st.markdown("---")
    st.caption("⏱ Data cached 30 s. Reload page to force-refresh.")

# ─────────────────────────────────────────────────────────────
# ░░ MAIN HEADER ░░
# ─────────────────────────────────────────────────────────────
st.markdown(
    "<h2 style='margin-bottom:4px'>EUR/USD Forex Bot — Control Center</h2>",
    unsafe_allow_html=True,
)
if st.session_state.paper_mode:
    st.markdown(
        "<div class='paper-banner'>📄 PAPER MODE ACTIVE — strategies running, no real orders</div>",
        unsafe_allow_html=True,
    )

# ─────────────────────────────────────────────────────────────
# ░░ TABS ░░
# ─────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "🟢 Live Overview",
    "📊 Strategy Comparison",
    "📋 Trade Journal",
    "🚨 Risk & Kill Switches",
])

# ═════════════════════════════════════════════════════════════
# TAB 1 — LIVE OVERVIEW
# ═════════════════════════════════════════════════════════════
with tab1:

    today      = _today_summary()
    open_list  = _open_trades()
    sess       = _session_info()
    next_news  = _next_high_impact()

    daily_pnl_val    = today["total_pnl_usd"]
    daily_trades_val = today["total_trades"]

    # Daily drawdown %
    daily_dd_pct = abs(daily_pnl_val) / account_balance * 100 if daily_pnl_val < 0 else 0.0

    # ── 4 metric cards ────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)

    pnl_delta_colour = "normal" if daily_pnl_val >= 0 else "inverse"
    c1.metric(
        "Today's PnL",
        f"${daily_pnl_val:+.2f}",
        delta=f"{daily_trades_val} trades",
        delta_color=pnl_delta_colour,
    )

    c2.metric("Open Trades", len(open_list),
              delta="positions" if open_list else "none open",
              delta_color="off")

    dd_label = f"{daily_dd_pct:.2f}%"
    c3.metric(
        "Daily Drawdown",
        dd_label,
        delta="⚠ above 3%" if daily_dd_pct > 3 else "within limit",
        delta_color="inverse" if daily_dd_pct > 3 else "normal",
    )

    c4.metric(
        "Current Session",
        sess["session"].upper(),
        delta=sess["current_time_est"],
        delta_color="off",
    )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Session Status box ────────────────────────────────────
    st.markdown("<div class='section-card'>", unsafe_allow_html=True)
    sc1, sc2, sc3 = st.columns([1.2, 1.5, 1.3])

    with sc1:
        st.markdown(f"**Session**")
        st.markdown(
            f"<span style='font-size:1.3rem;font-weight:700;color:{GREEN}'>"
            f"{sess['label']}</span>",
            unsafe_allow_html=True,
        )
        st.caption(f"{sess['start_time_est']} – {sess['end_time_est']} EST")

        market_col = GREEN if sess["market_open"] else RED
        market_txt = "OPEN" if sess["market_open"] else "CLOSED"
        st.markdown(
            f"<span style='color:{market_col};font-weight:700'>● Market {market_txt}</span>",
            unsafe_allow_html=True,
        )

    with sc2:
        st.markdown("**Active Strategies**")
        active = sess["active_strategies"]
        if active:
            for s in active:
                enabled = config.STRATEGIES.get(s, {}).get("enabled", False)
                dot = "🟢" if enabled else "⚫"
                st.markdown(f"{dot} `{s}`")
        else:
            st.caption("None active in this session")

    with sc3:
        st.markdown("**Next High-Impact News**")
        if next_news:
            mins_away = int(
                (next_news["time"] - datetime.now(timezone.utc)).total_seconds() / 60
            )
            colour = RED if mins_away < 60 else ORANGE if mins_away < 120 else GREEN
            st.markdown(
                f"<span style='color:{colour};font-weight:600'>"
                f"⚡ {next_news['event']}</span>",
                unsafe_allow_html=True,
            )
            st.caption(
                f"{next_news['currency']} | "
                f"{next_news['time'].strftime('%H:%M UTC')} "
                f"(in {mins_away} min)"
            )
        else:
            st.markdown(f"<span style='color:{GREEN}'>✅ None this week</span>",
                        unsafe_allow_html=True)

        news_status_txt = "✅ Safe to trade" if _news_safe() else "🚫 Blackout active"
        st.caption(news_status_txt)

    st.markdown("</div>", unsafe_allow_html=True)

    # ── Open Trades table ─────────────────────────────────────
    st.markdown("#### Open Positions")
    if open_list:
        open_df = pd.DataFrame(open_list)
        disp_cols = ["strategy", "direction", "entry_price", "stop_loss",
                     "take_profit", "lot_size", "session", "entry_time"]
        present   = [c for c in disp_cols if c in open_df.columns]
        open_disp = open_df[present].copy()
        open_disp.columns = [c.replace("_", " ").title() for c in present]

        # Duration column
        if "entry_time" in open_df.columns:
            open_disp["Duration"] = open_df["entry_time"].apply(
                lambda t: _fmt_duration(t)
            )

        st.dataframe(open_disp, use_container_width=True, hide_index=True)
    else:
        st.info("No open positions right now.")

    # ── Today's Trade Feed ────────────────────────────────────
    st.markdown("#### Today's Trade Feed")
    all_today = [
        t for t in _all_trades(100)
        if _is_today(t.get("entry_time") or t.get("exit_time"))
    ]

    if all_today:
        feed_rows = []
        for t in all_today[:10]:
            result = t.get("result", "OPEN")
            pnl    = float(t.get("pnl_usd") or 0)
            row_html = (
                f"<tr style='background:{_row_bg(result)}'>"
                f"<td>{_fmt_time(t.get('entry_time'))}</td>"
                f"<td><code>{t.get('strategy','')}</code></td>"
                f"<td>{t.get('direction','')}</td>"
                f"<td>{t.get('entry_price','')}</td>"
                f"<td>{t.get('exit_price') or '—'}</td>"
                f"<td>{_colour_pnl(pnl)}</td>"
                f"<td>{_result_badge(result)}</td>"
                f"</tr>"
            )
            feed_rows.append(row_html)

        st.markdown(
            f"""
            <table style='width:100%;border-collapse:collapse;font-size:0.85rem'>
              <thead>
                <tr style='color:{GREY};border-bottom:1px solid #2D3348'>
                  <th>Time</th><th>Strategy</th><th>Dir</th>
                  <th>Entry</th><th>Exit</th><th>PnL</th><th>Result</th>
                </tr>
              </thead>
              <tbody>{''.join(feed_rows)}</tbody>
            </table>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.info("No trades recorded today.")


# ═════════════════════════════════════════════════════════════
# TAB 2 — STRATEGY COMPARISON
# ═════════════════════════════════════════════════════════════
with tab2:

    # ── Strategy toggle panel ─────────────────────────────────
    st.markdown("#### Strategy Toggles")
    toggle_cols = st.columns(4)
    STRAT_LABELS = {
        "london_breakout": "London Breakout",
        "ict_smart_money": "ICT Smart Money",
        "asian_ny_range":  "Asian/NY Range",
        "mean_reversion":  "Mean Reversion",
    }

    for col, (name, label) in zip(toggle_cols, STRAT_LABELS.items()):
        key      = f"toggle_{name}"
        prev_val = st.session_state[key]
        new_val  = col.toggle(label, value=prev_val, key=f"_tog_{name}")

        if new_val != prev_val:
            # Persist to config in-memory
            config.STRATEGIES[name]["enabled"] = new_val
            st.session_state[key] = new_val
            action = "enabled ▶" if new_val else "disabled ⏹"
            col.success(f"{label} {action}") if new_val else col.warning(
                f"{label} disabled. No new trades will open."
            )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Comparison table ──────────────────────────────────────
    comparison = _comparison()

    if comparison:
        comp_df = pd.DataFrame(comparison)
        comp_df["status"] = comp_df["strategy"].apply(
            lambda s: "ACTIVE" if config.STRATEGIES.get(s, {}).get("enabled", False) else "DISABLED"
        )

        # Highlight best/worst PnL
        best_pnl_idx  = comp_df["total_pnl_usd"].idxmax()
        worst_pnl_idx = comp_df["total_pnl_usd"].idxmin()

        def _highlight(row):
            if row.name == best_pnl_idx:
                return [f"background-color: #0D2010; color: {GREEN}"] * len(row)
            if row.name == worst_pnl_idx and comp_df.loc[worst_pnl_idx, "total_pnl_usd"] < 0:
                return [f"background-color: #200D0D; color: {RED}"] * len(row)
            return [""] * len(row)

        display_df = comp_df[["strategy","total_trades","wins","losses",
                               "win_rate","avg_rr","total_pnl_usd",
                               "best_trade_usd","worst_trade_usd",
                               "avg_confidence","status"]].copy()
        display_df.columns = [
            "Strategy","Trades","Wins","Losses","Win %","Avg RR",
            "Total PnL","Best Trade","Worst Trade","Avg Conf","Status",
        ]
        display_df = display_df.sort_values("Total PnL", ascending=False)

        st.dataframe(
            display_df.style.apply(_highlight, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        # ── 4 bar charts ──────────────────────────────────────
        st.markdown("<br>", unsafe_allow_html=True)
        cc1, cc2, cc3, cc4 = st.columns(4)

        names = [s["strategy"].replace("_", " ").title() for s in comparison]

        with cc1:
            wr_vals = [s["win_rate"] for s in comparison]
            fig = _plotly_bar(
                pd.DataFrame({"Strategy": names, "Win Rate %": wr_vals}),
                x="Strategy", y="Win Rate %", title="Win Rate %",
                colors=[GREEN if v >= 50 else RED for v in wr_vals],
            )
            st.plotly_chart(fig, use_container_width=True)

        with cc2:
            pnl_vals = [s["total_pnl_usd"] for s in comparison]
            fig = _plotly_bar(
                pd.DataFrame({"Strategy": names, "Total PnL ($)": pnl_vals}),
                x="Strategy", y="Total PnL ($)", title="Total PnL ($)",
                colors=[GREEN if v >= 0 else RED for v in pnl_vals],
            )
            st.plotly_chart(fig, use_container_width=True)

        with cc3:
            rr_vals = [s["avg_rr"] for s in comparison]
            fig = _plotly_bar(
                pd.DataFrame({"Strategy": names, "Avg R:R": rr_vals}),
                x="Strategy", y="Avg R:R", title="Avg Risk:Reward",
                colors=[GREEN if v >= 1.0 else ORANGE for v in rr_vals],
            )
            st.plotly_chart(fig, use_container_width=True)

        with cc4:
            count_vals = [s["total_trades"] for s in comparison]
            fig = _plotly_bar(
                pd.DataFrame({"Strategy": names, "Trades": count_vals}),
                x="Strategy", y="Trades", title="Trade Count",
            )
            st.plotly_chart(fig, use_container_width=True)

        # ── Recommendation box ────────────────────────────────
        st.markdown("#### Auto Recommendations")
        _render_recommendations(comparison)

    else:
        st.info("No closed trades yet. Recommendations will appear after the first trade closes.")


# ═════════════════════════════════════════════════════════════
# TAB 3 — TRADE JOURNAL
# ═════════════════════════════════════════════════════════════
with tab3:

    # ── Filters ───────────────────────────────────────────────
    fc1, fc2, fc3, fc4, fc5 = st.columns([2, 1.5, 1.5, 1.5, 1.5])

    strategy_opts = ["All"] + list(config.STRATEGIES.keys())
    sel_strategy  = fc1.selectbox("Strategy", strategy_opts, index=0)
    sel_result    = fc2.selectbox("Result", ["All", "WIN", "LOSS", "OPEN"], index=0)
    sel_session   = fc3.selectbox("Session",
                                   ["All","london","newyork","asian","afternoon"],
                                   index=0)
    date_from = fc4.date_input("From", value=date.today() - timedelta(days=30))
    date_to   = fc5.date_input("To",   value=date.today())

    # ── Fetch & filter ────────────────────────────────────────
    all_raw = _all_trades(500)
    jdf = pd.DataFrame(all_raw) if all_raw else pd.DataFrame()

    if not jdf.empty:
        if sel_strategy != "All":
            jdf = jdf[jdf["strategy"] == sel_strategy]
        if sel_result != "All":
            jdf = jdf[jdf["result"] == sel_result]
        if sel_session != "All":
            jdf = jdf[jdf["session"] == sel_session]

        # Date filter
        if "entry_time" in jdf.columns:
            jdf["_date"] = pd.to_datetime(
                jdf["entry_time"], utc=True, errors="coerce"
            ).dt.date
            jdf = jdf[
                (jdf["_date"] >= date_from) & (jdf["_date"] <= date_to)
            ].drop(columns=["_date"])

        # ── Summary bar ───────────────────────────────────────
        tot = len(jdf)
        win_count = (jdf["result"] == "WIN").sum() if "result" in jdf.columns else 0
        total_pnl = jdf["pnl_usd"].sum() if "pnl_usd" in jdf.columns else 0.0
        wr_j      = round(win_count / tot * 100, 1) if tot else 0.0
        pnl_sign  = "+" if total_pnl >= 0 else ""

        st.markdown(
            f"<div class='section-card'>"
            f"Showing <b>{tot}</b> trades &nbsp;|&nbsp; "
            f"Total PnL: <b>{_colour_pnl(total_pnl)}</b> &nbsp;|&nbsp; "
            f"Win Rate: <b>{wr_j}%</b>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # ── Pagination ────────────────────────────────────────
        PAGE_SIZE = 20
        n_pages   = max(1, math.ceil(tot / PAGE_SIZE))
        page      = st.number_input("Page", min_value=1, max_value=n_pages, value=1)
        start_i   = (page - 1) * PAGE_SIZE
        page_df   = jdf.iloc[start_i: start_i + PAGE_SIZE].copy()

        # Columns to display
        show_cols = [c for c in [
            "strategy","direction","entry_price","exit_price",
            "stop_loss","take_profit","pnl_pips","pnl_usd",
            "risk_reward_actual","session","confidence_score",
            "entry_time","exit_time","result",
        ] if c in page_df.columns]
        page_disp = page_df[show_cols].copy()
        page_disp.columns = [c.replace("_", " ").title() for c in show_cols]

        # Colour the Result column
        def _style_result(val):
            colours = {"WIN": f"color:{GREEN};font-weight:600",
                       "LOSS": f"color:{RED};font-weight:600",
                       "OPEN": f"color:{GOLD};font-weight:600"}
            return colours.get(val, "")

        if "Result" in page_disp.columns:
            styled = page_disp.style.applymap(_style_result, subset=["Result"])
        else:
            styled = page_disp.style

        st.dataframe(styled, use_container_width=True, hide_index=True)
        st.caption(f"Page {page} / {n_pages}")

        # ── Export button ─────────────────────────────────────
        csv_buf = io.StringIO()
        jdf.to_csv(csv_buf, index=False)
        st.download_button(
            label     = "📥 Export to CSV",
            data      = csv_buf.getvalue().encode(),
            file_name = f"trades_{date_from}_{date_to}.csv",
            mime      = "text/csv",
        )
    else:
        st.info("No trades match the selected filters.")


# ═════════════════════════════════════════════════════════════
# TAB 4 — RISK & KILL SWITCHES
# ═════════════════════════════════════════════════════════════
with tab4:

    today_stats = _today_summary()
    daily_pnl_v = today_stats["total_pnl_usd"]
    kill_in_db  = DB.execute(
        text("SELECT kill_switch_triggered FROM daily_summary WHERE date=CURRENT_DATE")
    ).fetchone()
    kill_active = bool(kill_in_db and kill_in_db[0])

    # ── Kill banner if active ─────────────────────────────────
    if kill_active:
        st.markdown(
            "<div class='kill-banner'>🛑 KILL SWITCH TRIGGERED — All trading halted for today</div>",
            unsafe_allow_html=True,
        )

    # ── Daily risk meter ──────────────────────────────────────
    st.markdown("#### Daily Risk Meter")
    daily_loss_pct = abs(daily_pnl_v) / account_balance * 100 if daily_pnl_v < 0 else 0.0
    limit_pct      = config.RISK["max_daily_loss_pct"]

    rm1, rm2 = st.columns([2, 1])
    with rm1:
        _pct_bar(daily_loss_pct, limit_pct)
    with rm2:
        st.metric(
            "Today's PnL",
            f"${daily_pnl_v:+.2f}",
            delta=f"{daily_loss_pct:.2f}% of {limit_pct}% limit",
            delta_color="inverse" if daily_pnl_v < 0 else "normal",
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Emergency controls ────────────────────────────────────
    st.markdown("#### Emergency Controls")
    ec1, ec2, ec3 = st.columns([1.2, 1.2, 2])

    with ec1:
        if not kill_active:
            if st.button("🛑 STOP ALL TRADING", type="primary", use_container_width=True):
                st.session_state["confirm_stop"] = True

            if st.session_state.get("confirm_stop"):
                st.warning("Are you sure? This halts all trading for today.")
                yes, no = st.columns(2)
                if yes.button("✅ Yes, halt now", key="confirm_yes"):
                    DB.execute(text("""
                        INSERT INTO daily_summary (date, total_pnl_usd, total_trades, kill_switch_triggered)
                        VALUES (CURRENT_DATE, 0, 0, TRUE)
                        ON CONFLICT (date) DO UPDATE SET kill_switch_triggered = TRUE
                    """))
                    DB.commit()
                    st.session_state["confirm_stop"] = False
                    st.success("Kill switch activated. Bot will not open new trades today.")
                    st.cache_data.clear()
                    st.rerun()
                if no.button("❌ Cancel", key="confirm_no"):
                    st.session_state["confirm_stop"] = False
        else:
            st.markdown(
                f"<p style='color:{RED};font-weight:700'>🛑 Trading halted</p>",
                unsafe_allow_html=True,
            )

    with ec2:
        if kill_active:
            if st.button("▶️ RESUME TRADING", type="secondary", use_container_width=True):
                DB.execute(text("""
                    UPDATE daily_summary
                    SET kill_switch_triggered = FALSE
                    WHERE date = CURRENT_DATE
                """))
                DB.commit()
                st.success("Kill switch cleared. Trading will resume on next bot tick.")
                st.cache_data.clear()
                st.rerun()
        else:
            st.markdown(
                f"<span style='color:{GREEN}'>✅ Trading active</span>",
                unsafe_allow_html=True,
            )

    with ec3:
        st.markdown(
            "<div class='section-card' style='padding:10px'>"
            f"<b>Max daily loss:</b> {limit_pct}%<br>"
            f"<b>Max weekly loss:</b> {config.RISK['max_weekly_loss_pct']}%<br>"
            f"<b>Max open trades:</b> {config.RISK['max_open_trades']}"
            "</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Per-strategy risk settings ────────────────────────────
    st.markdown("#### Per-Strategy Risk Settings")
    risk_changed = False
    rs_cols = st.columns([2.5, 1.5, 1.5, 1])

    rs_cols[0].markdown("**Strategy**")
    rs_cols[1].markdown("**Risk % / trade**")
    rs_cols[2].markdown("**Min Confidence**")
    rs_cols[3].markdown("")

    for name, cfg in config.STRATEGIES.items():
        r1, r2, r3, r4 = st.columns([2.5, 1.5, 1.5, 1])
        r1.markdown(f"`{name}`")
        new_risk = r2.number_input(
            f"risk_{name}", label_visibility="collapsed",
            min_value=0.1, max_value=5.0,
            value=float(cfg["risk_per_trade"]), step=0.1,
            key=f"risk_input_{name}",
        )
        new_conf = r3.number_input(
            f"conf_{name}", label_visibility="collapsed",
            min_value=0.0, max_value=1.0,
            value=float(cfg["min_confidence"]), step=0.05,
            key=f"conf_input_{name}",
        )
        if new_risk != cfg["risk_per_trade"] or new_conf != cfg["min_confidence"]:
            risk_changed = True
            config.STRATEGIES[name]["risk_per_trade"] = new_risk
            config.STRATEGIES[name]["min_confidence"] = new_conf

    if risk_changed:
        st.success("✅ Risk settings updated in memory. Restart bot to persist permanently.")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Risk stats box ────────────────────────────────────────
    st.markdown("#### Risk Stats")
    closed_today = [
        t for t in _all_trades(200)
        if _is_today(t.get("exit_time")) and t.get("result") in ("WIN", "LOSS")
    ]

    if closed_today:
        pnl_vals     = [float(t.get("pnl_usd") or 0) for t in closed_today]
        largest_loss = min(pnl_vals)
        results      = [t.get("result") for t in closed_today]

        # Consecutive loss streak
        streak = 0
        for r in reversed(results):
            if r == "LOSS": streak += 1
            else: break

        # Weekly drawdown
        week_trades = [
            t for t in _all_trades(500)
            if _is_this_week(t.get("exit_time")) and t.get("result") in ("WIN","LOSS")
        ]
        week_pnl = sum(float(t.get("pnl_usd") or 0) for t in week_trades)
        week_dd  = abs(week_pnl) / account_balance * 100 if week_pnl < 0 else 0.0

        rs1, rs2, rs3, rs4 = st.columns(4)
        rs1.metric("Largest Loss Today",    f"${largest_loss:.2f}")
        rs2.metric("Consecutive Losses",    streak,
                   delta="streak" if streak > 0 else "none",
                   delta_color="inverse" if streak >= 3 else "off")
        rs3.metric("Weekly Drawdown",       f"{week_dd:.2f}%",
                   delta_color="inverse" if week_dd > 4 else "normal")

        # Prop firm check (FTMO-style 5% daily limit)
        ftmo_ok = daily_loss_pct < 5.0
        rs4.metric(
            "FTMO Daily Limit",
            "✅ Safe" if ftmo_ok else "🚫 Breached",
            delta=f"{daily_loss_pct:.2f}% / 5%",
            delta_color="normal" if ftmo_ok else "inverse",
        )
    else:
        st.info("No closed trades today — risk stats will appear here.")

    # ── Weekly risk meter ─────────────────────────────────────
    st.markdown("**Weekly Drawdown**")
    week_trades_all = [
        t for t in _all_trades(500)
        if _is_this_week(t.get("exit_time")) and t.get("result") in ("WIN","LOSS")
    ]
    week_pnl_all = sum(float(t.get("pnl_usd") or 0) for t in week_trades_all)
    week_dd_all  = abs(week_pnl_all) / account_balance * 100 if week_pnl_all < 0 else 0.0
    _pct_bar(week_dd_all, config.RISK["max_weekly_loss_pct"])


# ─────────────────────────────────────────────────────────────
# Helper functions (defined after tabs so they can reference
# module-level constants; Streamlit re-executes top to bottom)
# ─────────────────────────────────────────────────────────────
def _fmt_duration(entry_time) -> str:
    if entry_time is None:
        return "—"
    try:
        et = pd.to_datetime(entry_time, utc=True)
        delta = datetime.now(timezone.utc) - et.to_pydatetime()
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return "—"

def _fmt_time(t) -> str:
    if t is None: return "—"
    try:
        return pd.to_datetime(t, utc=True).strftime("%H:%M")
    except Exception:
        return "—"

def _row_bg(result: str) -> str:
    return {"WIN": "#0D2010", "LOSS": "#200D0D", "OPEN": "#1E1C0A"}.get(result, "")

def _is_today(t) -> bool:
    if t is None: return False
    try:
        return pd.to_datetime(t, utc=True).date() == date.today()
    except Exception:
        return False

def _is_this_week(t) -> bool:
    if t is None: return False
    try:
        dt = pd.to_datetime(t, utc=True).date()
        today = date.today()
        return (today - dt).days < 7
    except Exception:
        return False

def _render_recommendations(comparison: list[dict]) -> None:
    if not comparison:
        return

    lines  = []
    total  = sum(s["total_trades"] for s in comparison)
    best   = max(comparison, key=lambda s: s["total_pnl_usd"])
    worst  = min(comparison, key=lambda s: s["win_rate"])
    needed = max(0, 20 * len(comparison) - total)

    lines.append(
        f"✅ **{best['strategy']}** is your best strategy "
        f"({best['win_rate']:.1f}% win rate, "
        f"${best['total_pnl_usd']:+.2f} PnL)"
    )

    if worst["win_rate"] < 50 and worst["total_trades"] >= 5:
        lines.append(
            f"⚠️ **{worst['strategy']}** has the lowest win rate "
            f"({worst['win_rate']:.1f}%). "
            f"Consider disabling if under 45% after 20 trades."
        )

    if needed > 0:
        lines.append(
            f"📊 You need **{needed}** more trades for statistically "
            f"significant data (minimum 20 per strategy)."
        )

    for s in comparison:
        if s["avg_rr"] < 1.0 and s["total_trades"] >= 3:
            lines.append(
                f"📉 **{s['strategy']}** has avg RR below 1.0 ({s['avg_rr']:.2f}). "
                f"Review SL/TP settings."
            )

    st.markdown(
        "<div class='section-card'>" +
        "<br>".join(lines) +
        "</div>",
        unsafe_allow_html=True,
    )
