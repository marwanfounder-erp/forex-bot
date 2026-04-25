"""
core/trade_engine.py
Trade execution layer.

On Windows (MT5 available): sends live orders via MetaTrader5.
On Linux/Railway (MT5 unavailable): auto-activates PAPER MODE —
  trades are simulated, SL/TP hit detection uses yfinance current price,
  and all records are logged to Neon DB with notes="PAPER TRADE".
"""
from __future__ import annotations

import platform
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
import sqlalchemy

import config
from core import risk_manager as _rm
from core.database import Trade, get_session
from analytics.trade_logger import TradeLogger

# ── MT5 availability guard ─────────────────────────────────────────────────
MT5_AVAILABLE = platform.system() == "Windows"

if MT5_AVAILABLE:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        MT5_AVAILABLE = False
        mt5 = None
else:
    mt5 = None

PIP        = 0.0001
MAGIC      = 20250101
MAX_SPREAD = 3.0   # pips — skip if wider (live mode only)


class TradeEngine:
    """
    Parameters
    ----------
    data_feed    : core.data_feed instance (or mock)
    risk_manager : the core.risk_manager module (injectable for tests)
    db_session   : SQLAlchemy Session connected to Neon DB
    paper_mode   : force paper mode regardless of MT5 availability
    """

    def __init__(
        self,
        data_feed=None,
        risk_manager=None,
        db_session: Optional[Session] = None,
        paper_mode: bool = False,
    ) -> None:
        self.data_feed    = data_feed
        self.risk_manager = risk_manager or _rm
        self.db           = db_session or get_session()
        self.logger       = TradeLogger(self.db)

        # Paper mode: auto-on when MT5 unavailable OR explicitly forced
        self.paper_mode   = paper_mode or not MT5_AVAILABLE
        if self.paper_mode:
            import logging
            logging.getLogger(__name__).info(
                "TradeEngine running in PAPER MODE — no live orders will be placed"
            )

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
        Run pre-flight checks, place order (live or paper), log to Neon.

        Returns {"success": bool, "ticket": int|None, "reason": str}
        """
        symbol  = config.RISK["symbol"]
        balance = self._get_balance()

        # ── Check 1: risk limits ──────────────────────────────────────
        allowed, reason = self.risk_manager.can_trade(self.db, balance)
        if not allowed:
            return self._skip(f"Risk guard: {reason}")

        # ── Check 2: spread (live only) ───────────────────────────────
        if not self.paper_mode:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return self._skip("No tick data from MT5")
            spread_pips = round((tick.ask - tick.bid) / PIP, 1)
            if spread_pips > MAX_SPREAD:
                return self._skip(
                    f"Spread too wide: {spread_pips} pips (max {MAX_SPREAD})"
                )

        # ── Check 3: no duplicate same strategy + direction open ──────
        if self._has_open_trade(strategy_name, signal["signal"]):
            return self._skip(
                f"Duplicate open trade: {strategy_name} {signal['signal']}"
            )

        # ── Calculate lot size ────────────────────────────────────────
        sl_pips  = round(abs(signal["entry_price"] - signal["stop_loss"]) / PIP, 1)
        risk_pct = config.STRATEGIES.get(strategy_name, {}).get("risk_per_trade", 1.0)
        lot_size = self.risk_manager.calculate_lot_size(balance, risk_pct, sl_pips)

        # ── Place order ───────────────────────────────────────────────
        if self.paper_mode:
            ticket = self._paper_ticket()
            note   = "PAPER TRADE"
        else:
            mt5_result = self._send_mt5_order(signal, symbol, lot_size)
            if not mt5_result["success"]:
                return mt5_result
            ticket = mt5_result["ticket"]
            note   = None

        # ── Log to Neon DB ────────────────────────────────────────────
        signal_with_note = dict(signal)
        if note:
            signal_with_note["reason"] = note

        trade_id = self.logger.log_trade_open(
            signal      = signal_with_note,
            strategy    = strategy_name,
            session     = session_name,
            lot_size    = lot_size,
            mt5_ticket  = ticket,
        )

        return {
            "success":    True,
            "ticket":     ticket,
            "trade_id":   str(trade_id),
            "reason":     "Paper trade logged" if self.paper_mode else "Order placed",
            "lot_size":   lot_size,
            "sl_pips":    sl_pips,
            "paper_mode": self.paper_mode,
        }

    def monitor_open_trades(self) -> list[dict]:
        """
        Check open DB trades for SL/TP hits.

        Live mode:  compares against MT5 positions (removed = closed).
        Paper mode: compares entry/SL/TP against current yfinance price.

        Returns list of trades that were just closed this tick.
        """
        open_records = self.logger.get_open_db_trades()
        closed_now   = []

        for trade in open_records:
            if self.paper_mode or trade.get("notes") == "PAPER TRADE":
                result = self._check_paper_close(trade)
            else:
                result = self._check_live_close(trade)

            if result:
                closed_now.append(result)

        return closed_now

    def close_all_trades(self, reason: str = "emergency close") -> list[dict]:
        """
        Close every open position.

        Live mode:  sends close orders to MT5.
        Paper mode: force-closes all open DB records at current price.
        """
        if self.paper_mode:
            return self._paper_close_all(reason)

        symbol    = config.RISK["symbol"]
        positions = mt5.positions_get(symbol=symbol)
        results   = []

        if not positions:
            return results

        for pos in positions:
            tick = mt5.symbol_info_tick(pos.symbol)
            if pos.type == 0:
                close_type = 1
                price      = tick.bid
            else:
                close_type = 0
                price      = tick.ask

            req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       pos.symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "position":     pos.ticket,
                "price":        price,
                "deviation":    20,
                "magic":        MAGIC,
                "comment":      f"bot:{reason[:20]}",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res     = mt5.order_send(req)
            success = res and res.retcode == mt5.TRADE_RETCODE_DONE
            results.append({"ticket": pos.ticket, "success": success, "reason": reason})

        self.monitor_open_trades()
        return results

    def get_open_trades(self) -> list[dict]:
        """Return all currently OPEN trades from Neon DB."""
        return self.logger.get_open_db_trades()

    # ------------------------------------------------------------------ #
    # Live MT5 helpers                                                     #
    # ------------------------------------------------------------------ #

    def _send_mt5_order(self, signal: dict, symbol: str, lot_size: float) -> dict:
        tick  = mt5.symbol_info_tick(symbol)
        info  = mt5.symbol_info(symbol)
        digs  = info.digits if info else 5

        if signal["signal"] == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price      = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price      = tick.bid

        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       lot_size,
            "type":         order_type,
            "price":        price,
            "sl":           round(signal["stop_loss"],   digs),
            "tp":           round(signal["take_profit"], digs),
            "deviation":    10,
            "magic":        MAGIC,
            "comment":      signal.get("reason", "")[:30],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"success": True, "ticket": result.order}

        msg = result.comment if result else str(mt5.last_error())
        return {"success": False, "ticket": None, "reason": msg}

    def _check_live_close(self, trade: dict) -> Optional[dict]:
        ticket = trade.get("mt5_ticket")
        if not ticket:
            return None

        positions = mt5.positions_get(ticket=ticket)
        if positions:
            return None   # still open

        # Fetch close from deal history
        try:
            deals = mt5.history_deals_get(ticket=ticket)
            if not deals:
                return None
            last      = sorted(deals, key=lambda d: d.time)[-1]
            exit_price = float(last.price)
            exit_time  = datetime.fromtimestamp(last.time, tz=timezone.utc)
        except Exception:
            return None

        return self._close_trade_record(trade, exit_price, exit_time)

    def _get_balance(self) -> float:
        if MT5_AVAILABLE and mt5:
            info = mt5.account_info()
            return float(info.equity) if info else float(config.PAPER_BALANCE)
        return float(config.PAPER_BALANCE)

    # ------------------------------------------------------------------ #
    # Paper mode helpers                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _paper_ticket() -> int:
        """Generate a fake unique ticket number for paper trades."""
        import time
        return int(time.time() * 1000) % 2_147_483_647

    def _check_paper_close(self, trade: dict) -> Optional[dict]:
        """
        Simulate SL/TP hit detection using current yfinance price.
        Closes the paper trade if current price has crossed SL or TP.
        """
        try:
            from core.data_feed import get_current_price
            price_info  = get_current_price(config.RISK["symbol"])
            mid_price   = (price_info["bid"] + price_info["ask"]) / 2
        except Exception:
            return None

        entry     = trade.get("entry_price") or 0.0
        sl        = trade.get("stop_loss")   or 0.0
        tp        = trade.get("take_profit") or 0.0
        direction = trade.get("direction", "BUY")

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
        return self._close_trade_record(trade, exit_price, exit_time)

    def _close_trade_record(
        self,
        trade: dict,
        exit_price: float,
        exit_time: datetime,
    ) -> dict:
        direction_mult = 1.0 if trade["direction"] == "BUY" else -1.0
        pnl_pips  = round((exit_price - trade["entry_price"]) * direction_mult / PIP, 1)
        lot_size  = trade.get("lot_size") or 0.1
        pnl_usd   = round(pnl_pips * lot_size * 10.0, 2)
        sl_dist   = abs(trade["entry_price"] - trade["stop_loss"])
        sl_pips   = sl_dist / PIP if sl_dist > 0 else 1
        rr_actual = round(pnl_pips / sl_pips, 2)

        self.logger.log_trade_close(
            trade_id   = trade["id"],
            exit_price = exit_price,
            exit_time  = exit_time,
            pnl_pips   = pnl_pips,
            pnl_usd    = pnl_usd,
            rr_actual  = rr_actual,
        )

        return {**trade, "exit_price": exit_price,
                "pnl_pips": pnl_pips, "pnl_usd": pnl_usd, "rr_actual": rr_actual}

    def _paper_close_all(self, reason: str) -> list[dict]:
        """Force-close all open paper trades at current price."""
        open_records = self.logger.get_open_db_trades()
        results = []
        for trade in open_records:
            try:
                from core.data_feed import get_current_price
                price_info = get_current_price(config.RISK["symbol"])
                exit_price = (price_info["bid"] + price_info["ask"]) / 2
            except Exception:
                exit_price = trade.get("entry_price", 0.0)

            closed = self._close_trade_record(
                trade, exit_price, datetime.now(timezone.utc)
            )
            results.append({**closed, "reason": reason})
        return results

    # ------------------------------------------------------------------ #
    # Shared helpers                                                       #
    # ------------------------------------------------------------------ #

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
