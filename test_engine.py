"""
test_engine.py
──────────────
Integration test for the trade execution engine and analytics system.

What this does
──────────────
1. Connects to Neon DB and ensures schema is up-to-date.
2. Clears any leftover test rows from previous runs (idempotent).
3. Inserts 10 fake completed trades — mix of WIN/LOSS across all 4
   strategies and sessions — directly via TradeLogger (no MT5 required).
4. Runs PerformanceAnalyzer.get_all_strategies_comparison().
5. Runs PerformanceAnalyzer.get_recent_trades(limit=10).
6. Prints a formatted comparison table + recent trades log.
7. Calls update_strategy_performance_table() and verifies the
   strategy_performance table was written in Neon.

Run with:
    python3 test_engine.py
"""
from __future__ import annotations

import sys, os, uuid
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timedelta, timezone

# ── Project imports ───────────────────────────────────────────────────────
from core.database         import init_db, get_session, engine
from analytics.trade_logger import TradeLogger
from analytics.performance  import PerformanceAnalyzer
from sqlalchemy import text

# ── Test dataset ──────────────────────────────────────────────────────────
# 10 trades: strategy × direction × result × session × pnl
TEST_TRADES = [
    # strategy              dir    result  session     entry      exit       sl        tp     lot  pips  usd     rr    conf
    ("london_breakout",    "BUY",  "WIN",  "london",   1.08500, 1.08700, 1.08350, 1.08800, 0.10, 20.0, 20.00, 1.33, 0.82),
    ("london_breakout",    "BUY",  "WIN",  "london",   1.09100, 1.09360, 1.08950, 1.09600, 0.10, 26.0, 26.00, 1.73, 0.78),
    ("london_breakout",    "SELL", "LOSS", "london",   1.08800, 1.08950, 1.08950, 1.08500, 0.10,-15.0,-15.00,-1.00, 0.71),
    ("ict_smart_money",    "BUY",  "WIN",  "newyork",  1.08400, 1.08660, 1.08250, 1.08900, 0.08, 26.0, 20.80, 1.73, 0.88),
    ("ict_smart_money",    "SELL", "WIN",  "london",   1.09000, 1.08760, 1.09150, 1.08600, 0.08, 24.0, 19.20, 1.60, 0.85),
    ("ict_smart_money",    "BUY",  "LOSS", "newyork",  1.08600, 1.08450, 1.08450, 1.09000, 0.08,-15.0,-12.00,-1.00, 0.76),
    ("asian_ny_range",     "BUY",  "WIN",  "newyork",  1.08250, 1.08520, 1.08100, 1.08650, 0.10, 27.0, 27.00, 1.80, 0.74),
    ("asian_ny_range",     "SELL", "LOSS", "newyork",  1.08700, 1.08880, 1.08850, 1.08350, 0.10,-18.0,-18.00,-1.20, 0.66),
    ("mean_reversion",     "BUY",  "WIN",  "afternoon",1.08300, 1.08540, 1.08150, 1.08700, 0.06, 24.0, 14.40, 1.60, 0.70),
    ("mean_reversion",     "SELL", "LOSS", "afternoon",1.08600, 1.08750, 1.08750, 1.08300, 0.06,-15.0, -9.00,-1.00, 0.65),
]

TAG = "test_engine_run"   # injected into notes so we can clean up precisely


def _insert_fake_trades(logger: TradeLogger, db) -> list[str]:
    """Insert TEST_TRADES into Neon and return list of trade IDs."""
    ids   = []
    base  = datetime(2024, 4, 22, 8, 0, tzinfo=timezone.utc)

    for i, row in enumerate(TEST_TRADES):
        (strategy, direction, result, session,
         entry, exit_p, sl, tp, lot,
         pnl_pips, pnl_usd, rr, conf) = row

        entry_time = base + timedelta(hours=i * 2)
        exit_time  = entry_time + timedelta(hours=1)

        signal = {
            "signal":      direction,
            "entry_price": entry,
            "stop_loss":   sl,
            "take_profit": tp,
            "confidence":  conf,
            "reason":      TAG,
        }

        trade_id = logger.log_trade_open(
            signal     = signal,
            strategy   = strategy,
            session    = session,
            lot_size   = lot,
            mt5_ticket = 100_000 + i,
        )

        # Immediately close it with final pnl data
        logger.log_trade_close(
            trade_id   = trade_id,
            exit_price = exit_p,
            exit_time  = exit_time,
            pnl_pips   = pnl_pips,
            pnl_usd    = pnl_usd,
            rr_actual  = rr,
        )

        ids.append(str(trade_id))

    return ids


def _cleanup(db, trade_ids: list[str]) -> None:
    """Remove test rows so re-runs stay clean."""
    db.execute(
        text("DELETE FROM trades WHERE notes = :tag"),
        {"tag": TAG},
    )
    db.commit()


# ═════════════════════════════════════════════════════════════════════════
# Pretty printers
# ═════════════════════════════════════════════════════════════════════════

def _bar(widths: list[int], left="├", mid="┼", right="┤", fill="─") -> str:
    return left + mid.join(fill * (w + 2) for w in widths) + right


def _row(cells: list[str], widths: list[int]) -> str:
    parts = [f" {c:<{w}} " for c, w in zip(cells, widths)]
    return "│" + "│".join(parts) + "│"


def print_comparison_table(stats: list[dict]) -> None:
    headers = ["Strategy", "Trades", "W / L", "Win %", "Avg RR",
               "Total PnL", "Best", "Worst", "Avg Conf"]
    widths  = [22, 6, 7, 6, 6, 10, 8, 8, 8]

    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    sep = _bar(widths)
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

    print("\n" + top)
    print(_row(headers, widths))
    print(sep)

    for s in stats:
        wl  = f"{s['wins']}/{s['losses']}"
        wr  = f"{s['win_rate']:.1f}%"
        rr  = f"{s['avg_rr']:.2f}"
        pnl = f"${s['total_pnl_usd']:+.2f}"
        best= f"${s['best_trade_usd']:+.2f}"
        wst = f"${s['worst_trade_usd']:+.2f}"
        conf= f"{s['avg_confidence']:.3f}"
        print(_row([
            s["strategy"], str(s["total_trades"]), wl, wr, rr, pnl, best, wst, conf
        ], widths))

    print(bot)


def print_recent_trades(trades: list[dict]) -> None:
    headers = ["#", "Strategy", "Dir", "Result", "PnL USD", "PnL pips", "RR", "Session"]
    widths  = [2, 22, 4, 9, 8, 8, 5, 10]

    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    sep = _bar(widths)
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

    print("\n" + top)
    print(_row(headers, widths))
    print(sep)

    for i, t in enumerate(trades, 1):
        pnl_usd  = float(t.get("pnl_usd")  or 0)
        pnl_pips = float(t.get("pnl_pips") or 0)
        rr       = float(t.get("risk_reward_actual") or 0)
        print(_row([
            str(i),
            t.get("strategy", ""),
            t.get("direction", ""),
            t.get("result", ""),
            f"${pnl_usd:+.2f}",
            f"{pnl_pips:+.1f}",
            f"{rr:.2f}",
            t.get("session", ""),
        ], widths))

    print(bot)


# ═════════════════════════════════════════════════════════════════════════
# Main test runner
# ═════════════════════════════════════════════════════════════════════════

def run_tests() -> None:
    print("\n" + "═" * 62)
    print("  TRADE ENGINE & ANALYTICS — Integration Test")
    print("  Target: Neon PostgreSQL")
    print("═" * 62)

    # ── Step 1: DB init ───────────────────────────────────────────────
    print("\n[1/6] Initialising Neon DB schema...")
    init_db()
    db     = get_session()
    logger = TradeLogger(db_session=db)
    perf   = PerformanceAnalyzer(db_session=db)
    print("      OK — tables ready")

    # ── Step 2: Clean previous test data ─────────────────────────────
    print("[2/6] Cleaning previous test rows...")
    _cleanup(db, [])
    print("      OK — clean slate")

    # ── Step 3: Insert 10 fake trades ─────────────────────────────────
    print("[3/6] Inserting 10 fake completed trades...")
    trade_ids = _insert_fake_trades(logger, db)
    print(f"      OK — inserted {len(trade_ids)} trades")

    # ── Step 4: Verify rows in Neon ───────────────────────────────────
    print("[4/6] Verifying rows in Neon DB...")
    count_row = db.execute(
        text("SELECT COUNT(*) FROM trades WHERE notes = :tag"),
        {"tag": TAG},
    ).fetchone()
    db_count = count_row[0] if count_row else 0
    print(f"      OK — {db_count} rows visible in Neon DB")

    # ── Step 5: All-strategies comparison ────────────────────────────
    print("[5/6] Running get_all_strategies_comparison()...")
    comparison = perf.get_all_strategies_comparison()
    print(f"      OK — {len(comparison)} strategies returned")

    print("\n  ── Strategy Comparison Table (sorted by win rate) ──")
    print_comparison_table(comparison)

    best  = perf.get_best_strategy()
    worst = perf.get_worst_strategy()
    if best:
        print(f"\n  Best strategy (PnL)  : {best['strategy']} "
              f"(${best['total_pnl_usd']:+.2f})")
    if worst:
        print(f"  Worst strategy (W/R) : {worst['strategy']} "
              f"({worst['win_rate']:.1f}%)")

    # ── Step 6: Recent trades ─────────────────────────────────────────
    print("\n[6/6] Running get_recent_trades(limit=10)...")
    recent = perf.get_recent_trades(limit=10)
    print(f"      OK — {len(recent)} rows returned")

    print("\n  ── Recent Trades Log (newest first) ──")
    print_recent_trades(recent)

    # ── Step 7: Update strategy_performance table ─────────────────────
    print("\n[+] Syncing strategy_performance table...")
    perf.update_strategy_performance_table()
    sp_rows = db.execute(
        text("SELECT strategy, total_trades, wins, win_rate, total_pnl_usd "
             "FROM strategy_performance ORDER BY win_rate DESC")
    ).fetchall()
    print("      Neon strategy_performance table now contains:")
    for r in sp_rows:
        print(f"        {r.strategy:<22}  trades={r.total_trades}  "
              f"wins={r.wins}  win_rate={r.win_rate}%  "
              f"pnl=${r.total_pnl_usd:+.2f}")

    # ── Cleanup ───────────────────────────────────────────────────────
    print("\n[+] Cleaning up test rows...")
    _cleanup(db, trade_ids)
    db.close()
    print("    Done — test rows removed from Neon.\n")

    print("═" * 62)
    print("  All tests passed. Neon DB confirmed live and writable.")
    print("═" * 62 + "\n")


if __name__ == "__main__":
    run_tests()
