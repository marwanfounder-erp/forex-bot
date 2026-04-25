"""
analytics/performance.py
Queries Neon DB and computes per-strategy and aggregate performance metrics.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import StrategyPerformance, get_session


_EMPTY_STATS = {
    "strategy":          "",
    "total_trades":      0,
    "wins":              0,
    "losses":            0,
    "win_rate":          0.0,
    "avg_rr":            0.0,
    "total_pnl_usd":     0.0,
    "best_trade_usd":    0.0,
    "worst_trade_usd":   0.0,
    "max_drawdown":      0.0,
    "avg_confidence":    0.0,
    "trades_by_session": {},
}


class PerformanceAnalyzer:
    """
    Parameters
    ----------
    db_session : SQLAlchemy Session; if None a fresh session is opened.
    """

    def __init__(self, db_session: Optional[Session] = None) -> None:
        self.db = db_session or get_session()

    # ------------------------------------------------------------------ #
    # Per-strategy stats                                                   #
    # ------------------------------------------------------------------ #

    def get_strategy_stats(self, strategy_name: str) -> dict:
        """
        Full stats dict for one strategy, computed live from the trades table.
        """
        rows = self.db.execute(
            text("""
                SELECT pnl_usd, risk_reward_actual, result,
                       session, confidence_score, exit_time
                FROM trades
                WHERE strategy = :s AND result != 'OPEN'
                ORDER BY exit_time ASC
            """),
            {"s": strategy_name},
        ).fetchall()

        if not rows:
            return {**_EMPTY_STATS, "strategy": strategy_name}

        df = pd.DataFrame(rows, columns=[
            "pnl_usd", "rr", "result", "session", "confidence", "exit_time"
        ])

        wins   = (df["result"] == "WIN").sum()
        losses = (df["result"] == "LOSS").sum()
        total  = len(df)

        # Equity curve → running max → drawdown
        df["cumulative"] = df["pnl_usd"].cumsum()
        df["peak"]       = df["cumulative"].cummax()
        df["drawdown"]   = df["peak"] - df["cumulative"]
        max_drawdown     = round(df["drawdown"].max(), 2)

        trades_by_session = (
            df.groupby("session")["pnl_usd"]
            .count()
            .to_dict()
        )

        return {
            "strategy":          strategy_name,
            "total_trades":      total,
            "wins":              int(wins),
            "losses":            int(losses),
            "win_rate":          round(wins / total * 100, 1) if total else 0.0,
            "avg_rr":            round(df["rr"].dropna().mean(), 2) if not df["rr"].dropna().empty else 0.0,
            "total_pnl_usd":     round(df["pnl_usd"].sum(), 2),
            "best_trade_usd":    round(df["pnl_usd"].max(), 2),
            "worst_trade_usd":   round(df["pnl_usd"].min(), 2),
            "max_drawdown":      max_drawdown,
            "avg_confidence":    round(df["confidence"].dropna().mean(), 3),
            "trades_by_session": trades_by_session,
        }

    # ------------------------------------------------------------------ #
    # All-strategies comparison                                            #
    # ------------------------------------------------------------------ #

    def get_all_strategies_comparison(self) -> list[dict]:
        """
        Returns stats for ALL strategies in the trades table,
        sorted by win_rate descending. Single DB round-trip.
        """
        rows = self.db.execute(
            text("""
                SELECT
                    strategy,
                    COUNT(*)                                              AS total,
                    SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END)       AS wins,
                    SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END)       AS losses,
                    ROUND(AVG(CASE WHEN result!='OPEN' AND rn IS NOT NULL
                              THEN risk_reward_actual END)::numeric, 2)  AS avg_rr,
                    ROUND(SUM(pnl_usd)::numeric, 2)                      AS total_pnl,
                    ROUND(MAX(pnl_usd)::numeric, 2)                      AS best,
                    ROUND(MIN(pnl_usd)::numeric, 2)                      AS worst,
                    ROUND(AVG(confidence_score)::numeric, 3)             AS avg_conf
                FROM (
                    SELECT *, 1 AS rn FROM trades WHERE result != 'OPEN'
                ) sub
                GROUP BY strategy
                ORDER BY
                    CASE WHEN COUNT(*) > 0
                         THEN SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END)::float / COUNT(*)
                         ELSE 0
                    END DESC
            """)
        ).fetchall()

        result = []
        for r in rows:
            total = r.total or 0
            wins  = r.wins  or 0
            result.append({
                "strategy":       r.strategy,
                "total_trades":   total,
                "wins":           wins,
                "losses":         r.losses or 0,
                "win_rate":       round(wins / total * 100, 1) if total else 0.0,
                "avg_rr":         float(r.avg_rr or 0),
                "total_pnl_usd":  float(r.total_pnl or 0),
                "best_trade_usd": float(r.best or 0),
                "worst_trade_usd":float(r.worst or 0),
                "avg_confidence": float(r.avg_conf or 0),
            })
        return result

    # ------------------------------------------------------------------ #
    # Daily summary                                                        #
    # ------------------------------------------------------------------ #

    def get_daily_summary(self, target_date: date) -> dict:
        """Returns that day's aggregated performance."""
        row = self.db.execute(
            text("""
                SELECT
                    COALESCE(SUM(pnl_usd), 0)  AS pnl,
                    COUNT(*)                    AS trades,
                    strategy
                FROM trades
                WHERE result != 'OPEN'
                  AND DATE(exit_time AT TIME ZONE 'UTC') = :d
                GROUP BY strategy
                ORDER BY SUM(pnl_usd) DESC
                LIMIT 1
            """),
            {"d": target_date},
        ).fetchone()

        total_row = self.db.execute(
            text("""
                SELECT COALESCE(SUM(pnl_usd), 0) AS pnl,
                       COUNT(*) AS trades
                FROM trades
                WHERE result != 'OPEN'
                  AND DATE(exit_time AT TIME ZONE 'UTC') = :d
            """),
            {"d": target_date},
        ).fetchone()

        kill_row = self.db.execute(
            text("SELECT kill_switch_triggered FROM daily_summary WHERE date=:d"),
            {"d": target_date},
        ).fetchone()

        return {
            "date":                  str(target_date),
            "total_pnl_usd":         float(total_row.pnl) if total_row else 0.0,
            "total_trades":          int(total_row.trades) if total_row else 0,
            "best_strategy":         row.strategy if row else None,
            "kill_switch_triggered": bool(kill_row and kill_row[0]) if kill_row else False,
        }

    # ------------------------------------------------------------------ #
    # Recent trades                                                        #
    # ------------------------------------------------------------------ #

    def get_recent_trades(
        self, limit: int = 20, strategy: Optional[str] = None
    ) -> list[dict]:
        """Return last N closed trades, optionally filtered by strategy."""
        if strategy:
            rows = self.db.execute(
                text("""
                    SELECT id, mt5_ticket, strategy, direction, entry_price,
                           exit_price, lot_size, result, pnl_pips, pnl_usd,
                           risk_reward_actual, session, confidence_score,
                           entry_time, exit_time
                    FROM trades
                    WHERE result != 'OPEN' AND strategy = :s
                    ORDER BY exit_time DESC NULLS LAST
                    LIMIT :lim
                """),
                {"s": strategy, "lim": limit},
            ).fetchall()
        else:
            rows = self.db.execute(
                text("""
                    SELECT id, mt5_ticket, strategy, direction, entry_price,
                           exit_price, lot_size, result, pnl_pips, pnl_usd,
                           risk_reward_actual, session, confidence_score,
                           entry_time, exit_time
                    FROM trades
                    WHERE result != 'OPEN'
                    ORDER BY exit_time DESC NULLS LAST
                    LIMIT :lim
                """),
                {"lim": limit},
            ).fetchall()

        return [dict(r._mapping) for r in rows]

    # ------------------------------------------------------------------ #
    # strategy_performance table sync                                      #
    # ------------------------------------------------------------------ #

    def update_strategy_performance_table(self) -> None:
        """
        Full recalculate → upsert into strategy_performance table.
        Called after every trade close so the table is always current.
        """
        stats_list = self.get_all_strategies_comparison()
        now = datetime.now(timezone.utc)

        for stats in stats_list:
            self.db.execute(
                text("""
                    INSERT INTO strategy_performance
                        (strategy, total_trades, wins, losses, win_rate,
                         avg_rr, total_pnl_usd, max_drawdown, last_updated)
                    VALUES
                        (:strategy, :total, :wins, :losses, :wr,
                         :rr, :pnl, 0, :now)
                    ON CONFLICT (strategy) DO UPDATE SET
                        total_trades  = EXCLUDED.total_trades,
                        wins          = EXCLUDED.wins,
                        losses        = EXCLUDED.losses,
                        win_rate      = EXCLUDED.win_rate,
                        avg_rr        = EXCLUDED.avg_rr,
                        total_pnl_usd = EXCLUDED.total_pnl_usd,
                        last_updated  = EXCLUDED.last_updated
                """),
                {
                    "strategy": stats["strategy"],
                    "total":    stats["total_trades"],
                    "wins":     stats["wins"],
                    "losses":   stats["losses"],
                    "wr":       stats["win_rate"],
                    "rr":       stats["avg_rr"],
                    "pnl":      stats["total_pnl_usd"],
                    "now":      now,
                },
            )
        self.db.commit()

    # ------------------------------------------------------------------ #
    # Best / worst strategy helpers                                        #
    # ------------------------------------------------------------------ #

    def get_worst_strategy(self) -> Optional[dict]:
        """Strategy with the lowest win_rate (minimum 3 trades for relevance)."""
        stats = [s for s in self.get_all_strategies_comparison() if s["total_trades"] >= 3]
        if not stats:
            return None
        return min(stats, key=lambda s: s["win_rate"])

    def get_best_strategy(self) -> Optional[dict]:
        """Strategy with the highest total_pnl_usd."""
        stats = self.get_all_strategies_comparison()
        if not stats:
            return None
        return max(stats, key=lambda s: s["total_pnl_usd"])
