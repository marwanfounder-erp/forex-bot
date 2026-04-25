"""
main.py
Master bot loop — wires all components together and runs every 60 seconds.

Flags
-----
  --dry-run         Run N ticks then exit (default 3). No orders placed.
  --ticks N         Number of ticks for --dry-run (default 3).
"""
from __future__ import annotations

import argparse
import logging
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
from core.data_feed        import get_account_balance, get_ohlcv
from core.session_manager  import get_session_info, is_market_open
from core.news_filter      import NewsFilter
from core.trade_engine     import PaperTradeEngine
from analytics.trade_logger    import TradeLogger
from analytics.performance     import PerformanceAnalyzer
from strategies.strategy_router import StrategyRouter


class DataFeed:
    """Thin wrapper so StrategyRouter can call .get_ohlcv(...)."""
    def get_ohlcv(self, symbol, timeframe, bars=200):
        return get_ohlcv(symbol, timeframe, bars)


EST = pytz.timezone("America/New_York")

_tick_count = 0
PERF_UPDATE_EVERY = 5   # ticks (5 × 60 s = every 5 minutes)


# ═════════════════════════════════════════════════════════════════════════
# Master tick
# ═════════════════════════════════════════════════════════════════════════

def tick(
    engine:      PaperTradeEngine,
    news_filter: NewsFilter,
    router:      StrategyRouter,
    perf:        PerformanceAnalyzer,
    data_feed:   DataFeed,
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
        balance = config.PAPER_BALANCE

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
    for sig in signals:
        strategy_name = sig.get("strategy", "unknown")
        if dry_run:
            log.info(
                "[DRY-RUN] WOULD TRADE | %s %s | conf=%.2f | entry=%.5f sl=%.5f tp=%.5f",
                strategy_name, sig["signal"], sig["confidence"],
                sig["entry_price"], sig["stop_loss"], sig["take_profit"],
            )
        else:
            result = engine.execute_signal(sig, strategy_name, session)
            if result["success"]:
                log.info(
                    "[%s] [PAPER] ORDER | %s %s | ticket=%s | lot=%s | conf=%.2f",
                    now_est, strategy_name, sig["signal"],
                    result["ticket"], result.get("lot_size", "?"), sig["confidence"],
                )
            else:
                log.info("[%s] SKIP | %s: %s", now_est, strategy_name, result["reason"])

    # ── 6. Monitor existing positions ─────────────────────────────────
    if not dry_run:
        newly_closed = engine.monitor_open_trades()
        for t in newly_closed:
            outcome = "WIN" if t["pnl_usd"] > 0 else ("BREAKEVEN" if t["pnl_usd"] == 0 else "LOSS")
            log.info(
                "[%s] CLOSE %s | %s | PnL: $%+.2f (%+.1f pips) | RR: %.2f",
                now_est, outcome, t["strategy"],
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

    try:
        summary  = engine.get_account_summary()
        balance  = summary["balance"]
        today_pnl = summary["closed_pnl"]
    except Exception:
        balance   = config.PAPER_BALANCE
        today_pnl = today_stats["total_pnl_usd"]

    pnl_sign = "+" if today_pnl >= 0 else ""
    log.info(
        "[%s] Session: %-10s | Open: %d | Today: %s$%.2f | Balance: $%,.2f | Mode: PAPER",
        now_est, session, len(open_trades),
        pnl_sign, today_pnl, balance,
    )


# ═════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="EUR/USD Forex Bot — Paper Trading")
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
    log.info("  EUR/USD Forex Bot — Paper Trading Mode")
    log.info("  Starting balance: $%.2f", config.PAPER_BALANCE)
    if args.dry_run:
        log.info("  MODE: DRY-RUN (%d ticks)", args.ticks)
    log.info("=" * 62)

    # Init DB schema (idempotent)
    database.init_db()
    log.info("[DB] Neon DB ready")

    # Build shared components
    data_feed   = DataFeed()
    db          = database.get_session()
    news_filter = NewsFilter()
    router      = StrategyRouter(data_feed=data_feed)
    perf        = PerformanceAnalyzer(db_session=db)
    engine      = PaperTradeEngine(
        data_feed    = data_feed,
        risk_manager = risk_manager,
        db_session   = db,
    )

    log.info("[BOT] News: %s", news_filter.status_string())
    sess = get_session_info()
    log.info("[BOT] Session: %s | Market open: %s", sess["label"], sess["market_open"])
    log.info("[BOT] Mode: PAPER | Balance: $%.2f", config.PAPER_BALANCE)

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
        db.close()
        log.info("[BOT] Shutdown complete.")


if __name__ == "__main__":
    main()
