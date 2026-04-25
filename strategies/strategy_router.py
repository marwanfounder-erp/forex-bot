"""
strategies/strategy_router.py
Loads all strategies, filters by session, runs them, and ranks signals.
"""

from __future__ import annotations
import config
from strategies.london_breakout  import LondonBreakout
from strategies.ict_smart_money  import ICTSmartMoney
from strategies.asian_ny_range   import AsianNYRange
from strategies.mean_reversion   import MeanReversion
from strategies.base_strategy    import BaseStrategy


# Master registry — extend here when adding new strategies
_ALL_STRATEGIES: list[type[BaseStrategy]] = [
    LondonBreakout,
    ICTSmartMoney,
    AsianNYRange,
    MeanReversion,
]


class StrategyRouter:
    """
    Instantiates all registered strategies with the supplied data_feed,
    then orchestrates which ones run on each cycle.
    """

    def __init__(self, data_feed=None) -> None:
        self.data_feed = data_feed
        self.strategies: list[BaseStrategy] = [
            cls(data_feed=data_feed) for cls in _ALL_STRATEGIES
        ]

    # ------------------------------------------------------------------
    def get_active_strategies(self, session: str) -> list[BaseStrategy]:
        """
        Return every strategy that is both enabled in config AND
        whose session matches `session`.
        """
        return [s for s in self.strategies if s.is_active(session)]

    # ------------------------------------------------------------------
    def run_all(self, session: str, data_feed=None) -> list[dict]:
        """
        Run all active strategies for the current session.

        Steps:
          1. Collect signals from each active strategy.
          2. Drop NONE signals and signals below min_confidence.
          3. If multiple actionable signals fire simultaneously,
             keep only the one with the highest confidence score.

        Parameters
        ----------
        session   : current session name string (e.g. "newyork")
        data_feed : optional data_feed override (replaces instance-level)

        Returns
        -------
        List of signal dicts, sorted by confidence descending.
        At most one signal per direction is returned (highest confidence wins).
        """
        if data_feed is not None:
            # Hot-swap data feed for test/backtest scenarios
            for s in self.strategies:
                s.data_feed = data_feed

        active    = self.get_active_strategies(session)
        signals   = []

        for strategy in active:
            try:
                raw = strategy.generate_signal()
            except Exception as exc:
                print(f"  [{strategy.name}] ERROR during generate_signal: {exc}")
                continue

            if raw["signal"] == "NONE":
                continue

            min_conf = config.STRATEGIES.get(strategy.name, {}).get("min_confidence", 0.65)
            if raw["confidence"] < min_conf:
                continue

            # Tag with strategy name
            raw["strategy"] = strategy.name
            signals.append(raw)

        # Sort by confidence descending
        signals.sort(key=lambda s: s["confidence"], reverse=True)

        # Deduplicate: keep only highest-confidence signal per direction
        seen_directions: set[str] = set()
        final: list[dict] = []
        for sig in signals:
            if sig["signal"] not in seen_directions:
                seen_directions.add(sig["signal"])
                final.append(sig)

        return final

    # ------------------------------------------------------------------
    def status_summary(self, session: str) -> list[dict]:
        """
        Return a lightweight status dict per strategy for the dashboard.
        Does NOT run generate_signal — just reports enabled/active state.
        """
        return [
            {
                "name":    s.name,
                "enabled": s.enabled,
                "session": s.session,
                "active":  s.is_active(session),
            }
            for s in self.strategies
        ]
