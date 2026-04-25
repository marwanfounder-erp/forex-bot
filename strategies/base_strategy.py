"""
strategies/base_strategy.py
Abstract parent class for all trading strategies.
Every concrete strategy must inherit this and implement generate_signal().
"""

from __future__ import annotations
from abc import ABC, abstractmethod

import config


class BaseStrategy(ABC):
    """
    Parameters
    ----------
    data_feed : object  — instance of core.data_feed (or a mock in tests)
    cfg       : dict    — the full config.STRATEGIES[name] block; defaults
                          to the live config entry if not supplied explicitly
    """

    name: str = ""   # must be overridden; must match a key in config.STRATEGIES

    def __init__(self, data_feed=None, cfg: dict | None = None) -> None:
        self.data_feed = data_feed
        # Pull config from the live config module when not injected (allows
        # test code to pass in a custom dict for isolation)
        self._cfg: dict = cfg if cfg is not None else config.STRATEGIES.get(self.name, {})
        self.enabled: bool = bool(self._cfg.get("enabled", False))
        self.session: str  = self._cfg.get("session", "any")
        self.min_confidence: float = self._cfg.get("min_confidence", 0.65)
        self.risk_per_trade: float = self._cfg.get("risk_per_trade", 1.0)

    # ------------------------------------------------------------------
    # Abstract — must be implemented by every subclass
    # ------------------------------------------------------------------
    @abstractmethod
    def generate_signal(self) -> dict:
        """
        Analyse market data and return a signal dict.

        Must always return a dict with at least:
        {
            "signal":       "BUY" | "SELL" | "NONE",
            "confidence":   float,   # 0.0 – 1.0
            "entry_price":  float,
            "stop_loss":    float,
            "take_profit":  float,
            "reason":       str,     # human-readable explanation
        }
        """
        ...

    # ------------------------------------------------------------------
    # Session / enabled check
    # ------------------------------------------------------------------
    def is_active(self, current_session: str) -> bool:
        """
        Returns True when:
          • the strategy is enabled in config, AND
          • the current session matches the strategy's session
            (or the strategy's session is "any")
        """
        if not self.enabled:
            return False
        if self.session == "any":
            return True
        return current_session == self.session

    # ------------------------------------------------------------------
    # Utility helpers available to all strategies
    # ------------------------------------------------------------------
    @staticmethod
    def calculate_rr(entry: float, sl: float, tp: float) -> float:
        """
        Risk-reward ratio: reward / risk.

        Works for both long and short:
          risk   = abs(entry - sl)
          reward = abs(tp - entry)
        Returns 0.0 when risk is zero to avoid division errors.
        """
        risk   = abs(entry - sl)
        reward = abs(tp - entry)
        if risk == 0:
            return 0.0
        return round(reward / risk, 2)

    @staticmethod
    def _empty_signal(reason: str = "No signal") -> dict:
        """Convenience: return a NONE signal with a reason."""
        return {
            "signal":      "NONE",
            "confidence":  0.0,
            "entry_price": 0.0,
            "stop_loss":   0.0,
            "take_profit": 0.0,
            "reason":      reason,
        }

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"name={self.name!r} enabled={self.enabled} session={self.session!r}>"
        )
