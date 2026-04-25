"""
strategies/asian_ny_range.py

Asian Range / NY Breakout Strategy
─────────────────────────────────────
• Asian session (8pm–12am EST): track M15 high/low
• NY open (8am EST): wait for 2 consecutive M15 candles
  closing beyond the Asian level for confirmation
• Stop loss  : midpoint of Asian range
• Take profit: entry ± 2.5× stop distance (1:2.5 RR)
• Confidence : range size quality + breakout momentum +
               time of breakout (earlier in NY = higher)
"""

from __future__ import annotations
from datetime import date, datetime
import pandas as pd
import pytz

from strategies.base_strategy import BaseStrategy

EST = pytz.timezone("America/New_York")
PIP = 0.0001

# Ideal Asian range in pips (15–40 → full confidence weight)
IDEAL_RANGE_MIN = 15
IDEAL_RANGE_MAX = 40


class AsianNYRange(BaseStrategy):
    name = "asian_ny_range"

    # EST hour windows
    ASIAN_START = 20   # 8pm
    ASIAN_END   = 24   # midnight (wraps — 0 handled separately)
    NY_START    = 8    # 8am

    def __init__(self, data_feed=None, cfg=None) -> None:
        super().__init__(data_feed, cfg)
        self._asian_high:  float | None = None
        self._asian_low:   float | None = None
        self._range_date:  date  | None = None
        self._fired_date:  date  | None = None

    # ------------------------------------------------------------------
    def generate_signal(self) -> dict:
        df = self._get_data()
        if df is None or df.empty:
            return self._empty_signal("No M15 data available")

        self._update_asian_range(df)

        if self._asian_high is None or self._asian_low is None:
            return self._empty_signal("Asian range not yet built")

        today = datetime.now(EST).date()
        if self._fired_date == today:
            return self._empty_signal("Already fired today")

        # ── Must be in NY session to trade the breakout ───────────────
        current_hour = datetime.now(EST).hour
        if current_hour < self.NY_START:
            return self._empty_signal("Waiting for NY open")

        range_high  = self._asian_high
        range_low   = self._asian_low
        range_pips  = round((range_high - range_low) / PIP, 1)
        range_mid   = (range_high + range_low) / 2

        # ── Require 2 consecutive candles confirming the break ────────
        recent = df.tail(3)
        signal, breakout_candle = self._check_breakout_confirmation(
            recent, range_high, range_low
        )

        if signal == "NONE":
            return self._empty_signal(
                f"No confirmed breakout of range "
                f"({range_low:.5f}–{range_high:.5f})"
            )

        # ── Build trade levels ────────────────────────────────────────
        entry = breakout_candle["close"]
        if signal == "BUY":
            sl = range_mid
            tp = entry + 2.5 * (entry - sl)
        else:
            sl = range_mid
            tp = entry - 2.5 * (sl - entry)

        confidence = self._score_confidence(
            range_pips=range_pips,
            breakout_candle=breakout_candle,
            current_hour=current_hour,
        )

        if confidence < self.min_confidence:
            return self._empty_signal(
                f"Confidence {confidence:.2f} below minimum {self.min_confidence}"
            )

        self._fired_date = today

        return {
            "signal":      signal,
            "confidence":  confidence,
            "entry_price": round(entry, 5),
            "stop_loss":   round(sl, 5),
            "take_profit": round(tp, 5),
            "reason": (
                f"Asian/NY {signal} | Asian range {range_pips:.1f} pips "
                f"({range_low:.5f}–{range_high:.5f}) | "
                f"2-candle confirm | RR {self.calculate_rr(entry, sl, tp)}"
            ),
        }

    # ------------------------------------------------------------------
    # Dashboard helper
    # ------------------------------------------------------------------
    def get_asian_range(self) -> dict:
        """Return cached Asian range data for the dashboard."""
        if self._asian_high is None:
            return {"asian_high": None, "asian_low": None, "range_pips": None}
        pips = round((self._asian_high - self._asian_low) / PIP, 1)
        return {
            "asian_high": self._asian_high,
            "asian_low":  self._asian_low,
            "range_pips": pips,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    def _get_data(self) -> pd.DataFrame | None:
        if self.data_feed is None:
            return None
        if isinstance(self.data_feed, pd.DataFrame):
            return self.data_feed
        try:
            return self.data_feed.get_ohlcv("EURUSD", "M15", bars=200)
        except Exception:
            return None

    def _update_asian_range(self, df: pd.DataFrame) -> None:
        """Extract this session's Asian candles and update the cached range."""
        df = df.copy()
        if df["time"].dtype == object or not hasattr(df["time"].iloc[0], "tzinfo"):
            df["time"] = pd.to_datetime(df["time"], utc=True)
        df["est_hour"] = df["time"].dt.tz_convert(EST).dt.hour

        # Asian candles: 20–23 EST
        asian = df[df["est_hour"] >= self.ASIAN_START]
        if asian.empty:
            return

        self._asian_high = asian["high"].max()
        self._asian_low  = asian["low"].min()
        self._range_date = datetime.now(EST).date()

    def _check_breakout_confirmation(
        self,
        recent: pd.DataFrame,
        range_high: float,
        range_low:  float,
    ) -> tuple[str, pd.Series]:
        """
        Check for 2 consecutive candles closing beyond the range boundary.
        Returns (direction_string, confirming_candle) or ("NONE", last_candle).
        """
        candles = recent.reset_index(drop=True)
        last    = candles.iloc[-1]

        if len(candles) >= 2:
            prev = candles.iloc[-2]
            # Both recent candles closed above range_high → BUY
            if prev["close"] > range_high and last["close"] > range_high:
                return "BUY", last
            # Both recent candles closed below range_low → SELL
            if prev["close"] < range_low and last["close"] < range_low:
                return "SELL", last

        return "NONE", last

    def _score_confidence(
        self,
        range_pips: float,
        breakout_candle: pd.Series,
        current_hour: int,
    ) -> float:
        score = 0.0

        # Factor 1: range size quality (0.40 weight)
        if IDEAL_RANGE_MIN <= range_pips <= IDEAL_RANGE_MAX:
            score += 0.40
        elif 10 <= range_pips < IDEAL_RANGE_MIN or IDEAL_RANGE_MAX < range_pips <= 55:
            score += 0.25
        else:
            score += 0.05

        # Factor 2: breakout candle body momentum (0.35 weight)
        candle_range = breakout_candle["high"] - breakout_candle["low"]
        body         = abs(breakout_candle["close"] - breakout_candle["open"])
        body_pct     = (body / candle_range) if candle_range > 0 else 0
        score += 0.35 * min(body_pct / 0.65, 1.0)

        # Factor 3: time of breakout within NY session (0.25 weight)
        # Earlier = fresher breakout = higher confidence
        # 8am=max, 11am=min
        hours_into_ny = max(0, current_hour - self.NY_START)
        time_factor   = max(0, 1.0 - hours_into_ny / 3)
        score += 0.25 * time_factor

        return round(min(score, 1.0), 3)
