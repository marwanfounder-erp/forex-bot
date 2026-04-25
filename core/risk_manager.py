"""
core/risk_manager.py
Enforces daily/weekly loss limits, open-trade caps, and kill-switch logic.
All limits are sourced from config.RISK.
Reads live P&L data from Neon DB via the db session passed in.
"""

from __future__ import annotations
from datetime import datetime, date, timezone, timedelta
from sqlalchemy import text
from sqlalchemy.orm import Session

import config


# ---------------------------------------------------------------------------
# Lot-size calculation
# ---------------------------------------------------------------------------
def calculate_lot_size(
    account_balance: float,
    risk_pct: float,
    stop_loss_pips: float,
    pip_value_per_lot: float = 10.0,   # USD per pip per standard lot (EURUSD default)
) -> float:
    """
    Return the position size in lots.

    Formula:
        risk_amount = balance × risk_pct / 100
        lots        = risk_amount / (stop_loss_pips × pip_value_per_lot)

    Falls back to config default on invalid inputs.
    """
    if stop_loss_pips <= 0 or pip_value_per_lot <= 0:
        return config.RISK["default_lot_size"]

    risk_amount = account_balance * risk_pct / 100
    lots = risk_amount / (stop_loss_pips * pip_value_per_lot)
    lots = round(lots, 2)
    return max(lots, config.RISK["default_lot_size"])


# ---------------------------------------------------------------------------
# Daily limit
# ---------------------------------------------------------------------------
def check_daily_limit(db: Session, account_balance: float) -> tuple[bool, str]:
    """
    Reads today's closed-trade P&L from Neon DB.
    Returns (True, '') if safe to trade, (False, reason) if limit hit.
    """
    row = db.execute(
        text("""
            SELECT COALESCE(SUM(pnl_usd), 0)
            FROM trades
            WHERE result != 'OPEN'
              AND DATE(exit_time AT TIME ZONE 'UTC') = CURRENT_DATE
        """)
    ).fetchone()

    daily_pnl = float(row[0]) if row else 0.0
    limit = -(account_balance * config.RISK["max_daily_loss_pct"] / 100)

    if daily_pnl <= limit:
        return False, (
            f"Daily loss limit reached: ${daily_pnl:.2f} "
            f"(limit: ${limit:.2f})"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Weekly limit
# ---------------------------------------------------------------------------
def check_weekly_limit(db: Session, account_balance: float) -> tuple[bool, str]:
    """
    Reads this ISO week's closed-trade P&L from Neon DB.
    """
    row = db.execute(
        text("""
            SELECT COALESCE(SUM(pnl_usd), 0)
            FROM trades
            WHERE result != 'OPEN'
              AND DATE_TRUNC('week', exit_time AT TIME ZONE 'UTC')
                  = DATE_TRUNC('week', NOW() AT TIME ZONE 'UTC')
        """)
    ).fetchone()

    weekly_pnl = float(row[0]) if row else 0.0
    limit = -(account_balance * config.RISK["max_weekly_loss_pct"] / 100)

    if weekly_pnl <= limit:
        return False, (
            f"Weekly loss limit reached: ${weekly_pnl:.2f} "
            f"(limit: ${limit:.2f})"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Open-trade count
# ---------------------------------------------------------------------------
def can_open_trade(db: Session) -> tuple[bool, str]:
    """
    Returns (True, '') when the number of OPEN trades is below the configured cap.
    """
    row = db.execute(
        text("SELECT COUNT(*) FROM trades WHERE result = 'OPEN'")
    ).fetchone()

    open_count = int(row[0]) if row else 0
    max_trades = config.RISK["max_open_trades"]

    if open_count >= max_trades:
        return False, (
            f"Max open trades reached: {open_count}/{max_trades}"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

# In-process flag — prevents re-entry within the same bot run
_kill_switch_active: bool = False


def trigger_kill_switch(db: Session, reason: str) -> None:
    """
    Sets the in-process kill-switch flag AND records it in daily_summary.
    Once triggered, can_trade() will return False for the rest of the session.
    """
    global _kill_switch_active
    _kill_switch_active = True

    today = date.today()
    db.execute(
        text("""
            INSERT INTO daily_summary (date, total_pnl_usd, total_trades,
                                       max_drawdown_pct, kill_switch_triggered)
            VALUES (:d, 0, 0, 0, TRUE)
            ON CONFLICT (date) DO UPDATE
                SET kill_switch_triggered = TRUE
        """),
        {"d": today},
    )
    db.commit()
    print(f"[KILL SWITCH] Trading halted for today. Reason: {reason}")


def is_kill_switch_active(db: Session) -> bool:
    """Check both the in-process flag and today's DB row."""
    if _kill_switch_active:
        return True

    row = db.execute(
        text("""
            SELECT kill_switch_triggered
            FROM daily_summary
            WHERE date = CURRENT_DATE
        """)
    ).fetchone()

    return bool(row and row[0])


# ---------------------------------------------------------------------------
# Aggregate check
# ---------------------------------------------------------------------------
def can_trade(db: Session, account_balance: float) -> tuple[bool, str]:
    """
    Master guard — returns (True, '') only when ALL checks pass.
    Order: kill switch → daily limit → weekly limit → open-trade cap.
    """
    if is_kill_switch_active(db):
        return False, "Kill switch is active — trading halted for today"

    for check_fn, args in [
        (check_daily_limit,  (db, account_balance)),
        (check_weekly_limit, (db, account_balance)),
        (can_open_trade,     (db,)),
    ]:
        allowed, reason = check_fn(*args)
        if not allowed:
            # Auto-trigger kill switch on loss-limit breaches
            if "limit reached" in reason.lower():
                trigger_kill_switch(db, reason)
            return False, reason

    return True, ""
