"""
main.py
Master bot loop — wires all components together and runs every 60 seconds.

Flags
-----
  --dry-run         Run N ticks then exit (default 3). No live orders.
  --ticks N         Number of ticks for --dry-run (default 3).
"""
from __future__ import annotations

import argparse
import logging
import platform
import sys
import time
from datetime import date, datetime

import schedule
import pytz
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

import config
from core import database, risk_manager
from core.data_feed       import get_account_balance, get_ohlcv, MT5_AVAILABLE
from core.session_manager import get_session_info, is_market_open
from core.news_filter     import NewsFilter
from core.trade_engine    import TradeEngine
from analytics.trade_logger    import TradeLogger
from analytics.performance     import PerformanceAnalyzer
from strategies.strategy_router import StrategyRouter

# MT5 — Windows only
if MT5_AVAILABLE:
    import MetaTrader5 as mt5
else:
    mt5 = None


# ── MT5 data feed wrapper ─────────────────────────────────────────────────
class MT5DataFeed:
    """Thin wrapper so StrategyRouter can call .get_ohlcv(...)."""
    def get_ohlcv(self, symbol, timeframe, bars=200):
        return get_ohlcv(symbol, timeframe, bars)


EST = pytz.timezone("America/New_York")

# ── Performance update counter ────────────────────────────────────────────
_tick_count = 0
PERF_UPDATE_EVERY = 5   # ticks (5 × 60s = every 5 minutes)


# ═════════════════════════════════════════════════════════════════════════
# Startup
# ═════════════════════════════════════════════════════════════════════════

def connect_mt5() -> bool:
    """Connect to MT5 broker. Returns False on Linux (not available)."""
    if not MT5_AVAILABLE or mt5 is None:
        log.info("MT5 not available on %s — running in PAPER MODE", platform.system())
        return False

    if not mt5.initialize(
        login    = config.MT5_LOGIN,
        password = config.MT5_PASSWORD,
        server   = config.MT5_SERVER,
    ):
        log.error("MT5 connection failed: %s", mt5.last_error())
        return False

    info = mt5.account_info()
    log.info(
        "MT5 connected | Account: %s | Balance: %.2f %s",
        info.login, info.balance, info.currency,
    )
    return True


# ═════════════════════════════════════════════════════════════════════════
# Master tick
# ═════════════════════════════════════════════════════════════════════════

def tick(
    engine:      TradeEngine,
    news_filter: NewsFilter,
    router:      StrategyRouter,
    perf:        PerformanceAnalyzer,
    data_feed:   MT5DataFeed,
    db,
    dry_run:     bool = False,
) -> None:
    global _tick_count
    _tick_count += 1

    now_est   = datetime.now(EST).strftime("%H:%M EST")
    sess_info = get_session_info()
    session   = sess_info["session"]

    # ── 1. Market / session check ─────────────────────────────────────
    if not sess_info["market_open"]:
        log.info("[%s] Market closed (weekend) — skipping", now_est)
        return

    if session == "dead":
        log.info("[%s] Dead zone (5pm–8pm EST) — skipping", now_est)
        return

    # ── 2. News filter ────────────────────────────────────────────────
    if not news_filter.is_safe_to_trade():
        next_ev = news_filter.get_next_high_impact()
        label   = next_ev["event"] if next_ev else "unknown"
        log.info("[%s] News blackout — %s — skipping", now_est, label)
        return

    # ── 3. Risk check ─────────────────────────────────────────────────
    try:
        balance = get_account_balance()
    except Exception as exc:
        log.warning("[%s] Cannot get balance: %s", now_est, exc)
        return

    allowed, reason = risk_manager.can_trade(db, balance)
    if not allowed:
        log.info("[%s] Risk guard: %s", now_est, reason)
        return

    # ── 4. Run strategies ─────────────────────────────────────────────
    try:
        signals = router.run_all(session, data_feed)
    except Exception as exc:
        log.warning("[%s] Strategy error: %s", now_est, exc)
        signals = []

    # ── 5. Execute signals ────────────────────────────────────────────
    mode_tag = "[DRY-RUN]" if dry_run else ""
    for sig in signals:
        strategy_name = sig.get("strategy", "unknown")
        if dry_run:
            log.info(
                "%s WOULD TRADE | %s %s | conf=%.2f | entry=%.5f sl=%.5f tp=%.5f",
                mode_tag, strategy_name, sig["signal"], sig["confidence"],
                sig["entry_price"], sig["stop_loss"], sig["take_profit"],
            )
        else:
            result = engine.execute_signal(sig, strategy_name, session)
            if result["success"]:
                paper = " [PAPER]" if result.get("paper_mode") else ""
                log.info(
                    "[%s] ORDER%s | %s %s | ticket=%s | lot=%s | conf=%.2f",
                    now_est, paper, strategy_name, sig["signal"],
                    result["ticket"], result.get("lot_size", "?"), sig["confidence"],
                )
            else:
                log.info("[%s] SKIP | %s: %s", now_est, strategy_name, result["reason"])

    # ── 6. Monitor existing positions ─────────────────────────────────
    if not dry_run:
        newly_closed = engine.monitor_open_trades()
        for t in newly_closed:
            symbol = "WIN" if t["pnl_usd"] > 0 else "LOSS"
            log.info(
                "[%s] CLOSE %s | %s | PnL: $%+.2f (%+.1f pips) | RR: %.2f",
                now_est, symbol, t["strategy"],
                t["pnl_usd"], t["pnl_pips"], t["rr_actual"],
            )

    # ── 7. Performance table sync ─────────────────────────────────────
    if not dry_run and _tick_count % PERF_UPDATE_EVERY == 0:
        try:
            perf.update_strategy_performance_table()
        except Exception as exc:
            log.warning("[%s] Perf update error: %s", now_est, exc)

    # ── 8. Status line ────────────────────────────────────────────────
    open_trades = [] if dry_run else engine.get_open_trades()
    today_stats = perf.get_daily_summary(date.today())
    pnl_sign    = "+" if today_stats["total_pnl_usd"] >= 0 else ""

    paper_label = " | Mode: PAPER" if engine.paper_mode else " | Mode: LIVE"
    log.info(
        "[%s] Session: %-10s | News: safe | Open: %d | Today PnL: %s$%.2f (%d trades)%s",
        now_est, session, len(open_trades),
        pnl_sign, today_stats["total_pnl_usd"], today_stats["total_trades"],
        paper_label,
    )


# ═════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="EUR/USD Forex Bot")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Run N ticks then exit without placing orders",
    )
    p.add_argument(
        "--ticks", type=int, default=3,
        help="Number of ticks for --dry-run (default: 3)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    log.info("=" * 62)
    log.info("  EUR/USD Forex Bot — Starting up")
    if args.dry_run:
        log.info("  MODE: DRY-RUN (%d ticks)", args.ticks)
    log.info("=" * 62)

    # Init DB schema (idempotent)
    database.init_db()
    log.info("[DB] Neon DB ready")

    # Connect MT5 (no-op on Linux — returns False, paper mode activates)
    live_mt5 = connect_mt5()

    # Build shared components
    data_feed   = MT5DataFeed()
    db          = database.get_session()
    news_filter = NewsFilter()
    router      = StrategyRouter(data_feed=data_feed)
    perf        = PerformanceAnalyzer(db_session=db)
    engine      = TradeEngine(
        data_feed    = data_feed,
        risk_manager = risk_manager,
        db_session   = db,
        paper_mode   = args.dry_run or not live_mt5,
    )

    log.info("[BOT] News: %s", news_filter.status_string())
    sess = get_session_info()
    log.info("[BOT] Session: %s | Market open: %s", sess["label"], sess["market_open"])
    log.info("[BOT] Paper mode: %s", engine.paper_mode)

    if args.dry_run:
        log.info("[BOT] Running %d dry-run ticks then exiting\n", args.ticks)
        for i in range(1, args.ticks + 1):
            log.info("── Dry-run tick %d/%d ──────────────────────", i, args.ticks)
            tick(engine, news_filter, router, perf, data_feed, db, dry_run=True)
            if i < args.ticks:
                time.sleep(2)
        log.info("\n[BOT] Dry-run complete — exiting.")
        db.close()
        return

    log.info("[BOT] Starting master loop (60s interval)\n")

    def _tick():
        tick(engine, news_filter, router, perf, data_feed, db)

    schedule.every(60).seconds.do(_tick)
    _tick()   # run immediately on start

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("\n[BOT] Stopped by user.")
    finally:
        engine.close_all_trades("bot_shutdown")
        if live_mt5 and mt5:
            mt5.shutdown()
        db.close()
        log.info("[MT5] Connection closed.")


if __name__ == "__main__":
    main()
