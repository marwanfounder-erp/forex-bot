"""
strategies/london_breakout.py

London Breakout Strategy
─────────────────────────
• Consolidation window : 2am–7am EST (M15 candles)
• Signal              : 7am candle close above range_high → BUY
                        7am candle close below range_low  → SELL
• Stop loss           : opposite side of range + 5-pip buffer
• Take profit         : entry ± 2× stop distance (1:2 RR)
• Fires once per day  : _fired_date tracks today's signal
• Confidence factors  : range size (ideal 10–25 pips), breakout
                        candle body strength, volume spike
"""

from __future__ import annotations
from datetime import date, datetime, timezone
import pandas as pd
import pytz

from strategies.base_strategy import BaseStrategy

EST       = pytz.timezone("America/New_York")
PIP       = 0.0001      # 1 pip for EURUSD
BUFFER    = 5 * PIP     # 5-pip SL buffer beyond range edge


class LondonBreakout(BaseStrategy):
    name = "london_breakout"

    # EST hours that define the consolidation / build-up window
    CONSOL_START = 2   # inclusive
    CONSOL_END   = 7   # exclusive  (7am candle IS the breakout check)

    def __init__(self, data_feed=None, cfg=None) -> None:
        super().__init__(data_feed, cfg)
        self._fired_date: date | None = None   # prevent multiple signals per day
        # Cache for dashboard display
        self._last_range: dict = {}

    # ------------------------------------------------------------------
    # Core signal logic
    # ------------------------------------------------------------------
    def generate_signal(self) -> dict:
        today = datetime.now(EST).date()
        if self._fired_date == today:
            return self._empty_signal("Already fired today")

        df = self._get_data()
        if df is None or df.empty:
            return self._empty_signal("No M15 data available")

        # ── Build consolidation range (2am–7am EST) ──────────────────
        consol = self._consolidation_candles(df)
        if len(consol) < 4:   # need at least 4 × M15 = 1 hour of data
            return self._empty_signal("Insufficient consolidation candles")

        range_high = consol["high"].max()
        range_low  = consol["low"].min()
        range_pips = round((range_high - range_low) / PIP, 1)

        self._last_range = {
            "range_high": range_high,
            "range_low":  range_low,
            "range_pips": range_pips,
        }

        # ── Check latest candle (should be around / just after 7am) ──
        latest = df.iloc[-1]
        close  = latest["close"]

        if close <= range_high and close >= range_low:
            return self._empty_signal(
                f"Price inside range ({range_low:.5f}–{range_high:.5f})"
            )

        # ── Determine direction ───────────────────────────────────────
        is_long = close > range_high

        if is_long:
            entry = close
            sl    = range_low  - BUFFER
            tp    = entry + 2 * (entry - sl)
        else:
            entry = close
            sl    = range_high + BUFFER
            tp    = entry - 2 * (sl - entry)

        # ── Confidence score ─────────────────────────────────────────
        confidence = self._score_confidence(
            range_pips=range_pips,
            breakout_candle=latest,
            consol_df=consol,
        )

        if confidence < self.min_confidence:
            return self._empty_signal(
                f"Confidence {confidence:.2f} below minimum {self.min_confidence}"
            )

        self._fired_date = today
        direction = "BUY" if is_long else "SELL"

        return {
            "signal":      direction,
            "confidence":  confidence,
            "entry_price": round(entry, 5),
            "stop_loss":   round(sl, 5),
            "take_profit": round(tp, 5),
            "reason": (
                f"London breakout {direction} | range {range_pips:.1f} pips "
                f"({range_low:.5f}–{range_high:.5f}) | "
                f"RR {self.calculate_rr(entry, sl, tp)}"
            ),
        }

    # ------------------------------------------------------------------
    # Dashboard helper
    # ------------------------------------------------------------------
    def get_range_info(self) -> dict:
        """Return cached range data for dashboard display."""
        return self._last_range.copy()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    def _get_data(self) -> pd.DataFrame | None:
        if self.data_feed is None:
            return None
        try:
            import MetaTrader5 as mt5
            return self.data_feed.get_ohlcv("EURUSD", "M15", bars=60)
        except Exception:
            # In test mode data_feed is a DataFrame directly
            return self.data_feed if isinstance(self.data_feed, pd.DataFrame) else None

    def _consolidation_candles(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter candles whose EST hour falls in [CONSOL_START, CONSOL_END)."""
        df = df.copy()
        if not hasattr(df["time"].iloc[0], "tzinfo") or df["time"].iloc[0].tzinfo is None:
            df["time"] = pd.to_datetime(df["time"], utc=True)
        df["est_hour"] = df["time"].dt.tz_convert(EST).dt.hour
        return df[df["est_hour"].between(self.CONSOL_START, self.CONSOL_END - 1)]

    def _score_confidence(
        self,
        range_pips: float,
        breakout_candle: pd.Series,
        consol_df: pd.DataFrame,
    ) -> float:
        score = 0.0

        # Factor 1: range size (ideal 10–25 pips → full 0.4 weight)
        if 10 <= range_pips <= 25:
            score += 0.40
        elif 5 <= range_pips < 10 or 25 < range_pips <= 40:
            score += 0.25
        elif range_pips < 5:
            score += 0.0    # too tight, probably noise
        else:
            score += 0.10   # very wide range → lower quality

        # Factor 2: breakout candle body strength (body / full_range)
        candle_range = breakout_candle["high"] - breakout_candle["low"]
        body         = abs(breakout_candle["close"] - breakout_candle["open"])
        body_ratio   = (body / candle_range) if candle_range > 0 else 0
        score += 0.35 * min(body_ratio / 0.6, 1.0)  # 0.6 body ratio = full points

        # Factor 3: volume spike vs consolidation average
        avg_vol = consol_df["volume"].mean()
        if avg_vol > 0:
            vol_ratio = breakout_candle["volume"] / avg_vol
            score += 0.25 * min(vol_ratio / 1.5, 1.0)  # 1.5× avg = full points
        else:
            score += 0.10   # no volume data, partial credit

        return round(min(score, 1.0), 3)
