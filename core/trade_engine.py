"""
core/trade_engine.py
Paper trading engine — simulates real trading using live EUR/USD price data.

All trades are tracked in memory and persisted to Neon DB.
No MT5 or live broker connection — 100% paper mode.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import sqlalchemy
from sqlalchemy.orm import Session

import config
from core import risk_manager as _rm
from core.database import Trade, get_session
from analytics.trade_logger import TradeLogger

log = logging.getLogger(__name__)

PIP        = 0.0001
MAX_SPREAD = 3.0   # pips — informational only in paper mode


class PaperTradeEngine:
    """
    Paper trading engine.

    Simulates order execution using live EUR/USD prices from data_feed.
    All trades are persisted to Neon DB with notes="PAPER TRADE".
    Account starts at PAPER_BALANCE (default $10,000) and tracks equity
    based on closed P&L.

    Parameters
    ----------
    data_feed    : core.data_feed module (or mock for tests)
    risk_manager : core.risk_manager module (injectable for tests)
    db_session   : SQLAlchemy Session connected to Neon DB
    """

    # Keep paper_mode=True as a read-only property so calling code that checks
    # engine.paper_mode still works without modification.
    paper_mode = True

    def __init__(
        self,
        data_feed=None,
        risk_manager=None,
        db_session: Optional[Session] = None,
        **_kwargs,           # absorb any legacy keyword args (e.g. paper_mode=True)
    ) -> None:
        self.data_feed    = data_feed
        self.risk_manager = risk_manager or _rm
        self.db           = db_session or get_session()
        self.logger       = TradeLogger(self.db)
        self._starting_balance = float(config.PAPER_BALANCE)
        log.info("PaperTradeEngine initialised | Starting balance: $%.2f", self._starting_balance)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def execute_signal(
        self,
        signal: dict,
        strategy_name: str,
        session_name: str,
    ) -> dict:
        """
        Run pre-flight checks and open a paper trade.

        Returns {"success": bool, "ticket": int|None, "reason": str, ...}
        """
        symbol  = config.RISK["symbol"]
        balance = self._get_balance()

        # ── Check 1: risk limits ──────────────────────────────────────
        allowed, reason = self.risk_manager.can_trade(self.db, balance)
        if not allowed:
            return self._skip(f"Risk guard: {reason}")

        # ── Check 2: no duplicate same strategy + direction open ──────
        if self._has_open_trade(strategy_name, signal["signal"]):
            return self._skip(
                f"Duplicate open trade: {strategy_name} {signal['signal']}"
            )

        # ── Fetch live entry price ────────────────────────────────────
        try:
            from core.data_feed import get_current_price
            price_info  = get_current_price(symbol)
            entry_price = (price_info["bid"] + price_info["ask"]) / 2
        except Exception as exc:
            return self._skip(f"Cannot get live price: {exc}")

        # Override signal entry with live price so we're realistic
        live_signal = dict(signal)
        live_signal["entry_price"] = entry_price

        # ── Calculate lot size ────────────────────────────────────────
        sl_pips  = round(abs(entry_price - signal["stop_loss"]) / PIP, 1)
        risk_pct = config.STRATEGIES.get(strategy_name, {}).get("risk_per_trade", 1.0)
        lot_size = self.risk_manager.calculate_lot_size(balance, risk_pct, sl_pips)

        # ── Create paper trade ────────────────────────────────────────
        ticket = self._paper_ticket()
        live_signal["reason"] = "PAPER TRADE"

        trade_id = self.logger.log_trade_open(
            signal     = live_signal,
            strategy   = strategy_name,
            session    = session_name,
            lot_size   = lot_size,
            mt5_ticket = ticket,
        )

        log.info(
            "[PAPER] %s %s @ %.5f | SL %.5f | TP %.5f | lots=%.2f | %s",
            signal["signal"], symbol, entry_price,
            signal["stop_loss"], signal["take_profit"],
            lot_size, strategy_name,
        )

        return {
            "success":    True,
            "ticket":     ticket,
            "trade_id":   str(trade_id),
            "reason":     "Paper trade logged",
            "lot_size":   lot_size,
            "sl_pips":    sl_pips,
            "paper_mode": True,
        }

    def monitor_open_trades(self) -> list[dict]:
        """
        Check all open DB trades for SL/TP hits or 48-hour timeout.

        Returns list of trades closed this tick.
        """
        open_records = self.logger.get_open_db_trades()
        closed_now   = []

        for trade in open_records:
            result = self._check_paper_close(trade)
            if result:
                closed_now.append(result)

        return closed_now

    def get_open_trades(self) -> list[dict]:
        """Return all currently OPEN trades from Neon DB."""
        return self.logger.get_open_db_trades()

    def close_all_trades(self, reason: str = "emergency close") -> list[dict]:
        """Force-close all OPEN paper trades at current live price, marked BREAKEVEN."""
        open_records = self.logger.get_open_db_trades()
        results      = []

        for trade in open_records:
            try:
                from core.data_feed import get_current_price
                price_info = get_current_price(config.RISK["symbol"])
                exit_price = (price_info["bid"] + price_info["ask"]) / 2
            except Exception:
                exit_price = trade.get("entry_price", 0.0)

            closed = self._close_trade_record(
                trade, exit_price, datetime.now(timezone.utc), force_breakeven=True
            )
            results.append({**closed, "reason": reason})

        return results

    def get_account_summary(self) -> dict:
        """
        Return paper account summary.

        {
            balance:    float   ← starting_balance + all closed P&L
            equity:     float   ← balance + unrealised open P&L
            open_pnl:   float   ← sum of unrealised P&L on open trades
            closed_pnl: float   ← sum of today's closed trade P&L
        }
        """
        from core.data_feed import get_current_price

        # All-time closed P&L → current balance
        try:
            closed_trades = self.logger.get_all_trades(limit=10_000)
            total_closed_pnl = sum(
                float(t.get("pnl_usd") or 0)
                for t in closed_trades
                if t.get("result") in ("WIN", "LOSS", "BREAKEVEN")
            )
        except Exception:
            total_closed_pnl = 0.0

        balance = self._starting_balance + total_closed_pnl

        # Today's closed P&L
        today = datetime.now(timezone.utc).date()
        today_closed_pnl = sum(
            float(t.get("pnl_usd") or 0)
            for t in (closed_trades if 'closed_trades' in dir() else [])  # reuse if available
            if t.get("result") in ("WIN", "LOSS", "BREAKEVEN")
            and _is_today(t.get("exit_time"), today)
        )

        # Unrealised P&L on open trades
        open_pnl = 0.0
        try:
            open_records = self.logger.get_open_db_trades()
            price_info   = get_current_price(config.RISK["symbol"])
            mid          = (price_info["bid"] + price_info["ask"]) / 2
            for t in open_records:
                entry     = float(t.get("entry_price") or 0)
                lot_size  = float(t.get("lot_size") or 0.01)
                direction = t.get("direction", "BUY")
                mult      = 1.0 if direction == "BUY" else -1.0
                pips      = (mid - entry) * mult / PIP
                open_pnl += pips * lot_size * 10.0
        except Exception:
            pass

        return {
            "balance":    round(balance, 2),
            "equity":     round(balance + open_pnl, 2),
            "open_pnl":   round(open_pnl, 2),
            "closed_pnl": round(today_closed_pnl, 2),
        }

    # ------------------------------------------------------------------ #
    # Paper mode helpers                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _paper_ticket() -> int:
        """Generate a unique fake ticket number."""
        import time
        return int(time.time() * 1000) % 2_147_483_647

    def _check_paper_close(self, trade: dict) -> Optional[dict]:
        """
        Simulate SL/TP hit or 48-hour timeout using live price.
        Returns closed trade dict or None if still open.
        """
        try:
            from core.data_feed import get_current_price
            price_info = get_current_price(config.RISK["symbol"])
            mid_price  = (price_info["bid"] + price_info["ask"]) / 2
        except Exception:
            return None

        entry     = float(trade.get("entry_price") or 0)
        sl        = float(trade.get("stop_loss")   or 0)
        tp        = float(trade.get("take_profit") or 0)
        direction = trade.get("direction", "BUY")

        # Check 48-hour timeout
        open_time = trade.get("entry_time")
        if open_time:
            if isinstance(open_time, str):
                open_time = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            if open_time.tzinfo is None:
                open_time = open_time.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - open_time
            if age > timedelta(hours=48):
                log.info(
                    "[PAPER] Trade %s expired after 48h — closing BREAKEVEN",
                    trade.get("id", "?")
                )
                return self._close_trade_record(
                    trade, mid_price, datetime.now(timezone.utc), force_breakeven=True
                )

        # Check SL/TP
        hit_sl = hit_tp = False
        if direction == "BUY":
            hit_sl = mid_price <= sl
            hit_tp = mid_price >= tp
        else:
            hit_sl = mid_price >= sl
            hit_tp = mid_price <= tp

        if not (hit_sl or hit_tp):
            return None

        exit_price = tp if hit_tp else sl
        exit_time  = datetime.now(timezone.utc)
        label      = "TP HIT" if hit_tp else "SL HIT"
        log.info("[PAPER] %s | %s %s | exit %.5f", label, direction, trade.get("strategy",""), exit_price)
        return self._close_trade_record(trade, exit_price, exit_time)

    def _close_trade_record(
        self,
        trade: dict,
        exit_price: float,
        exit_time: datetime,
        force_breakeven: bool = False,
    ) -> dict:
        direction_mult = 1.0 if trade["direction"] == "BUY" else -1.0
        pnl_pips  = round((exit_price - float(trade["entry_price"])) * direction_mult / PIP, 1)
        lot_size  = float(trade.get("lot_size") or 0.01)
        pnl_usd   = round(pnl_pips * lot_size * 10.0, 2)
        sl_dist   = abs(float(trade["entry_price"]) - float(trade["stop_loss"]))
        sl_pips   = sl_dist / PIP if sl_dist > 0 else 1
        rr_actual = round(pnl_pips / sl_pips, 2)

        if force_breakeven:
            pnl_usd = 0.0
            pnl_pips = 0.0
            rr_actual = 0.0

        self.logger.log_trade_close(
            trade_id   = trade["id"],
            exit_price = exit_price,
            exit_time  = exit_time,
            pnl_pips   = pnl_pips,
            pnl_usd    = pnl_usd,
            rr_actual  = rr_actual,
        )

        return {
            **trade,
            "exit_price": exit_price,
            "pnl_pips":   pnl_pips,
            "pnl_usd":    pnl_usd,
            "rr_actual":  rr_actual,
        }

    # ------------------------------------------------------------------ #
    # Shared helpers                                                       #
    # ------------------------------------------------------------------ #

    def _get_balance(self) -> float:
        """Current paper balance = starting + all closed P&L."""
        try:
            closed = self.logger.get_all_trades(limit=10_000)
            total  = sum(
                float(t.get("pnl_usd") or 0)
                for t in closed
                if t.get("result") in ("WIN", "LOSS", "BREAKEVEN")
            )
            return self._starting_balance + total
        except Exception:
            return self._starting_balance

    def _has_open_trade(self, strategy: str, direction: str) -> bool:
        row = self.db.execute(
            sqlalchemy.text(
                "SELECT 1 FROM trades "
                "WHERE strategy=:s AND direction=:d AND result='OPEN' LIMIT 1"
            ),
            {"s": strategy, "d": direction},
        ).fetchone()
        return row is not None

    @staticmethod
    def _skip(reason: str) -> dict:
        return {"success": False, "ticket": None, "reason": reason}


def _is_today(dt, today=None) -> bool:
    if dt is None:
        return False
    if today is None:
        today = datetime.now(timezone.utc).date()
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return False
    if hasattr(dt, "date"):
        return dt.date() == today
    return False


# Alias so any code that still imports TradeEngine continues to work
TradeEngine = PaperTradeEngine
