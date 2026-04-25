"""
analytics/trade_logger.py
Writes and reads trade records in the Neon PostgreSQL database.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import Trade, DailySummary, get_session


class TradeLogger:
    """
    Parameters
    ----------
    db_session : SQLAlchemy Session; if None a fresh session is opened.
    """

    def __init__(self, db_session: Optional[Session] = None) -> None:
        self.db = db_session or get_session()

    # ------------------------------------------------------------------ #
    # Open trade                                                           #
    # ------------------------------------------------------------------ #

    def log_trade_open(
        self,
        signal:     dict,
        strategy:   str,
        session:    str,
        lot_size:   float,
        mt5_ticket: Optional[int] = None,
    ) -> uuid.UUID:
        """
        Insert a new trade row with result='OPEN'.
        Returns the generated UUID trade ID.
        """
        trade_id = uuid.uuid4()
        trade = Trade(
            id               = trade_id,
            mt5_ticket       = mt5_ticket,
            strategy         = strategy,
            symbol           = "EURUSD",
            direction        = signal["signal"],          # "BUY" or "SELL"
            entry_price      = signal.get("entry_price"),
            exit_price       = None,
            stop_loss        = signal.get("stop_loss"),
            take_profit      = signal.get("take_profit"),
            lot_size         = lot_size,
            result           = "OPEN",
            pnl_pips         = 0.0,
            pnl_usd          = 0.0,
            risk_reward_actual = None,
            session          = session,
            confidence_score = signal.get("confidence"),
            entry_time       = datetime.now(timezone.utc),
            exit_time        = None,
            notes            = signal.get("reason", ""),
        )
        self.db.add(trade)
        self.db.commit()
        return trade_id

    # ------------------------------------------------------------------ #
    # Close trade                                                          #
    # ------------------------------------------------------------------ #

    def log_trade_close(
        self,
        trade_id:   uuid.UUID | str,
        exit_price: float,
        exit_time:  datetime,
        pnl_pips:   float,
        pnl_usd:    float,
        rr_actual:  float,
    ) -> None:
        """
        Update an existing trade row with close data and calculate result.
        result = 'WIN' if pnl_usd > 0, 'LOSS' if < 0, else 'BREAKEVEN'.
        """
        if pnl_usd > 0:
            result = "WIN"
        elif pnl_usd < 0:
            result = "LOSS"
        else:
            result = "BREAKEVEN"

        if isinstance(trade_id, str):
            trade_id = uuid.UUID(trade_id)

        self.db.execute(
            text("""
                UPDATE trades
                SET exit_price         = :ep,
                    exit_time          = :et,
                    pnl_pips           = :pp,
                    pnl_usd            = :pu,
                    risk_reward_actual = :rr,
                    result             = :res
                WHERE id = :tid
            """),
            {
                "ep":  exit_price,
                "et":  exit_time,
                "pp":  pnl_pips,
                "pu":  pnl_usd,
                "rr":  rr_actual,
                "res": result,
                "tid": str(trade_id),
            },
        )
        self.db.commit()

    # ------------------------------------------------------------------ #
    # Kill switch                                                          #
    # ------------------------------------------------------------------ #

    def log_kill_switch(self, reason: str, daily_pnl: float) -> None:
        """
        Upsert today's daily_summary row with kill_switch_triggered=True.
        """
        today = date.today()
        self.db.execute(
            text("""
                INSERT INTO daily_summary
                    (date, total_pnl_usd, total_trades, max_drawdown_pct, kill_switch_triggered)
                VALUES (:d, :pnl, 0, 0, TRUE)
                ON CONFLICT (date) DO UPDATE
                    SET total_pnl_usd        = :pnl,
                        kill_switch_triggered = TRUE
            """),
            {"d": today, "pnl": daily_pnl},
        )
        self.db.commit()
        print(f"[KILL SWITCH] Logged — reason: {reason} | daily PnL: ${daily_pnl:.2f}")

    # ------------------------------------------------------------------ #
    # Queries                                                              #
    # ------------------------------------------------------------------ #

    def get_open_db_trades(self) -> list[dict]:
        """Return all trades with result='OPEN'."""
        rows = self.db.execute(
            text("SELECT * FROM trades WHERE result='OPEN' ORDER BY entry_time ASC")
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_all_trades(self, limit: int = 500) -> list[dict]:
        """Return recent trades, newest first."""
        rows = self.db.execute(
            text("SELECT * FROM trades ORDER BY entry_time DESC LIMIT :lim"),
            {"lim": limit},
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_dict(row) -> dict:
        """Convert a SQLAlchemy Row into a plain dict."""
        return dict(row._mapping)
