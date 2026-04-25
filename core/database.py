"""
core/database.py
Neon PostgreSQL connection and schema bootstrap.
All tables are created on first import via init_db().
"""

from __future__ import annotations
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, text,
    Column, String, Float, Integer, Boolean, Date, Text,
    DateTime, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Session
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Engine — pool_pre_ping tests the connection before every checkout so stale
# SSL connections (Neon idles out in ~5 min) are replaced transparently.
# pool_recycle forces a new connection after 4 min to stay under that limit.
# ---------------------------------------------------------------------------
_DATABASE_URL: str = os.getenv("NEON_DATABASE_URL", "")

if not _DATABASE_URL:
    raise EnvironmentError(
        "NEON_DATABASE_URL is not set. "
        "Add it to your .env file: NEON_DATABASE_URL=postgresql://user:pass@host/db"
    )

engine = create_engine(
    _DATABASE_URL,
    pool_pre_ping   = True,   # SELECT 1 before checkout — replaces dead connections
    pool_recycle    = 240,    # recycle connections every 4 min (Neon idles at ~5 min)
    pool_size       = 3,
    max_overflow    = 2,
    connect_args    = {
        "keepalives":          1,
        "keepalives_idle":     30,   # seconds before sending keepalive probe
        "keepalives_interval": 10,   # seconds between probes
        "keepalives_count":    5,    # probes before declaring connection dead
        "connect_timeout":     10,
    },
    echo=False,
)


# ---------------------------------------------------------------------------
# ORM base + models
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mt5_ticket          = Column(Integer,     nullable=True)   # MT5 position ticket
    strategy            = Column(String(64),  nullable=False)
    symbol              = Column(String(16),  nullable=False, default="EURUSD")
    direction           = Column(String(8),   nullable=False)          # BUY / SELL
    entry_price         = Column(Float,       nullable=True)
    exit_price          = Column(Float,       nullable=True)
    stop_loss           = Column(Float,       nullable=True)
    take_profit         = Column(Float,       nullable=True)
    lot_size            = Column(Float,       nullable=True)
    result              = Column(String(16),  nullable=False, default="OPEN")  # WIN/LOSS/BREAKEVEN/OPEN
    pnl_pips            = Column(Float,       nullable=True, default=0.0)
    pnl_usd             = Column(Float,       nullable=True, default=0.0)
    risk_reward_actual  = Column(Float,       nullable=True)
    session             = Column(String(32),  nullable=True)           # london/newyork/asian/afternoon
    confidence_score    = Column(Float,       nullable=True)
    entry_time          = Column(DateTime(timezone=True), nullable=True)
    exit_time           = Column(DateTime(timezone=True), nullable=True)
    notes               = Column(Text,        nullable=True)


class StrategyPerformance(Base):
    __tablename__ = "strategy_performance"

    strategy        = Column(String(64), primary_key=True)
    total_trades    = Column(Integer, default=0)
    wins            = Column(Integer, default=0)
    losses          = Column(Integer, default=0)
    win_rate        = Column(Float,   default=0.0)
    avg_rr          = Column(Float,   default=0.0)
    total_pnl_usd   = Column(Float,   default=0.0)
    max_drawdown    = Column(Float,   default=0.0)
    last_updated    = Column(DateTime(timezone=True), nullable=True)


class DailySummary(Base):
    __tablename__ = "daily_summary"

    date                    = Column(Date,    primary_key=True)
    total_pnl_usd           = Column(Float,   default=0.0)
    total_trades            = Column(Integer, default=0)
    max_drawdown_pct        = Column(Float,   default=0.0)
    best_strategy           = Column(String(64), nullable=True)
    kill_switch_triggered   = Column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Create all tables if they don't already exist, then apply any column migrations."""
    Base.metadata.create_all(engine)
    # Idempotent column migrations for tables that already existed
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS mt5_ticket INTEGER"
        ))


def get_session() -> Session:
    """Return a new SQLAlchemy ORM session. Caller is responsible for closing."""
    return Session(engine)


def test_connection() -> dict:
    """
    Verify connectivity and return server version info.
    Returns {"ok": True, "version": "..."} or {"ok": False, "error": "..."}.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version()")).fetchone()
        return {"ok": True, "version": row[0]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Convenience write helpers (used by trade_engine / risk_manager)
# ---------------------------------------------------------------------------
def upsert_strategy_performance(strategy: str, win: bool, rr: float, pnl_usd: float) -> None:
    """Increment counters on strategy_performance after a trade closes."""
    with get_session() as session:
        row = session.get(StrategyPerformance, strategy)
        if row is None:
            row = StrategyPerformance(strategy=strategy)
            session.add(row)

        row.total_trades = (row.total_trades or 0) + 1
        if win:
            row.wins = (row.wins or 0) + 1
        else:
            row.losses = (row.losses or 0) + 1

        row.total_pnl_usd = round((row.total_pnl_usd or 0.0) + pnl_usd, 2)
        total = row.total_trades or 1
        row.win_rate     = round((row.wins or 0) / total * 100, 1)
        row.avg_rr       = round(((row.avg_rr or 0.0) * (total - 1) + rr) / total, 2)
        row.last_updated = datetime.now(timezone.utc)
        session.commit()


def upsert_daily_summary(date_val, pnl_usd: float, kill_switch: bool = False, best_strategy: str | None = None) -> None:
    """Upsert today's daily_summary row."""
    with get_session() as session:
        row = session.get(DailySummary, date_val)
        if row is None:
            row = DailySummary(date=date_val)
            session.add(row)

        row.total_pnl_usd  = round((row.total_pnl_usd or 0.0) + pnl_usd, 2)
        row.total_trades   = (row.total_trades or 0) + 1
        if kill_switch:
            row.kill_switch_triggered = True
        if best_strategy:
            row.best_strategy = best_strategy
        session.commit()
