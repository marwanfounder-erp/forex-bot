"""
strategies/ict_smart_money.py

ICT Smart Money Concept Strategy
──────────────────────────────────
Signal fires when ALL three conditions align:
  1. Liquidity Sweep   — wick below swing low / above swing high, then reversal
  2. Order Block (H1)  — price retraces into the last valid OB
  3. Fair Value Gap    — unmitigated FVG exists in the same zone

Confidence = weighted count of aligned conditions (all three = highest).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
import pandas as pd

from strategies.base_strategy import BaseStrategy

PIP = 0.0001


# ── Small data containers ─────────────────────────────────────────────────
@dataclass
class FVG:
    kind:   Literal["bullish", "bearish"]
    top:    float
    bottom: float

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class OrderBlock:
    kind:       Literal["bullish", "bearish"]
    ob_high:    float
    ob_low:     float
    candle_idx: int


# ── Strategy ──────────────────────────────────────────────────────────────
class ICTSmartMoney(BaseStrategy):
    name = "ict_smart_money"

    SWING_LOOKBACK = 20   # candles for swing high/low detection
    STRONG_MOVE_N  = 3    # consecutive candles = "strong move"
    MAX_FVG_STORE  = 5    # keep last N valid FVGs

    def __init__(self, data_feed=None, cfg=None) -> None:
        super().__init__(data_feed, cfg)

    # ------------------------------------------------------------------
    def generate_signal(self) -> dict:
        m15 = self._get_df("M15")
        h1  = self._get_df("H1")

        if m15 is None or h1 is None or len(m15) < 40 or len(h1) < 20:
            return self._empty_signal("Insufficient data")

        # ── Detections ───────────────────────────────────────────────
        fvgs         = self._detect_fvgs(m15)
        order_blocks = self._detect_order_blocks(h1)
        sweep        = self._detect_liquidity_sweep(m15)

        if sweep is None:
            return self._empty_signal("No liquidity sweep detected")

        # ── Filter OBs and FVGs matching sweep direction ─────────────
        direction = sweep["direction"]   # "bullish" or "bearish"

        matching_ob  = self._find_matching_ob(order_blocks, sweep, m15)
        matching_fvg = self._find_matching_fvg(fvgs, sweep, m15)

        conditions_met = sum([
            True,                       # sweep is always true here
            matching_ob  is not None,
            matching_fvg is not None,
        ])

        confidence = self._score_confidence(conditions_met, sweep, matching_ob, matching_fvg)

        if confidence < self.min_confidence:
            return self._empty_signal(
                f"Only {conditions_met}/3 conditions met (conf={confidence:.2f})"
            )

        # ── Build levels ─────────────────────────────────────────────
        current_price = m15["close"].iloc[-1]

        if direction == "bullish":
            entry = current_price
            sl    = (matching_ob.ob_low - 5 * PIP) if matching_ob else (sweep["sweep_price"] - 10 * PIP)
            tp    = self._next_liquidity_level(m15, "bullish")
        else:
            entry = current_price
            sl    = (matching_ob.ob_high + 5 * PIP) if matching_ob else (sweep["sweep_price"] + 10 * PIP)
            tp    = self._next_liquidity_level(m15, "bearish")

        signal = "BUY" if direction == "bullish" else "SELL"

        parts = ["Liquidity sweep"]
        if matching_ob:  parts.append("OB confluence")
        if matching_fvg: parts.append("FVG confluence")

        return {
            "signal":      signal,
            "confidence":  confidence,
            "entry_price": round(entry, 5),
            "stop_loss":   round(sl, 5),
            "take_profit": round(tp, 5),
            "reason":      (
                f"ICT {signal} | {' + '.join(parts)} | "
                f"conditions {conditions_met}/3 | "
                f"RR {self.calculate_rr(entry, sl, tp)}"
            ),
        }

    # ------------------------------------------------------------------
    # FVG detection — stores last MAX_FVG_STORE valid gaps
    # ------------------------------------------------------------------
    def _detect_fvgs(self, df: pd.DataFrame) -> list[FVG]:
        fvgs: list[FVG] = []
        for i in range(2, len(df)):
            c1, c3 = df.iloc[i - 2], df.iloc[i]
            # Bullish FVG: gap up between c1.high and c3.low
            if c3["low"] > c1["high"]:
                fvgs.append(FVG("bullish", top=c3["low"], bottom=c1["high"]))
            # Bearish FVG: gap down between c1.low and c3.high
            elif c3["high"] < c1["low"]:
                fvgs.append(FVG("bearish", top=c1["low"], bottom=c3["high"]))
        return fvgs[-self.MAX_FVG_STORE:]

    # ------------------------------------------------------------------
    # Order block detection on H1
    # ------------------------------------------------------------------
    def _detect_order_blocks(self, df: pd.DataFrame) -> list[OrderBlock]:
        obs: list[OrderBlock] = []
        n = self.STRONG_MOVE_N

        for i in range(1, len(df) - n):
            # Check for n consecutive bullish candles after candle i
            window = df.iloc[i + 1 : i + 1 + n]
            if len(window) < n:
                break

            all_bull = all(window["close"] > window["open"])
            all_bear = all(window["close"] < window["open"])

            c = df.iloc[i]
            if all_bull and c["close"] < c["open"]:   # last bearish before bull move
                obs.append(OrderBlock("bullish", ob_high=c["high"], ob_low=c["low"], candle_idx=i))
            elif all_bear and c["close"] > c["open"]:  # last bullish before bear move
                obs.append(OrderBlock("bearish", ob_high=c["high"], ob_low=c["low"], candle_idx=i))

        return obs[-10:]   # keep last 10

    # ------------------------------------------------------------------
    # Liquidity sweep detection on M15
    # ------------------------------------------------------------------
    def _detect_liquidity_sweep(self, df: pd.DataFrame) -> dict | None:
        lb = self.SWING_LOOKBACK
        if len(df) < lb + 2:
            return None

        window = df.iloc[-(lb + 2):-2]
        last   = df.iloc[-1]

        swing_high = window["high"].max()
        swing_low  = window["low"].min()

        # Bullish sweep: wick below swing low, close back above it
        if last["low"] < swing_low and last["close"] > swing_low:
            return {"direction": "bullish", "sweep_price": last["low"]}

        # Bearish sweep: wick above swing high, close back below it
        if last["high"] > swing_high and last["close"] < swing_high:
            return {"direction": "bearish", "sweep_price": last["high"]}

        return None

    # ------------------------------------------------------------------
    # Match helpers
    # ------------------------------------------------------------------
    def _find_matching_ob(
        self, obs: list[OrderBlock], sweep: dict, df: pd.DataFrame
    ) -> OrderBlock | None:
        price = df["close"].iloc[-1]
        for ob in reversed(obs):
            if sweep["direction"] == "bullish" and ob.kind == "bullish":
                if ob.ob_low <= price <= ob.ob_high:
                    return ob
            elif sweep["direction"] == "bearish" and ob.kind == "bearish":
                if ob.ob_low <= price <= ob.ob_high:
                    return ob
        return None

    def _find_matching_fvg(
        self, fvgs: list[FVG], sweep: dict, df: pd.DataFrame
    ) -> FVG | None:
        price = df["close"].iloc[-1]
        for fvg in reversed(fvgs):
            if sweep["direction"] == "bullish" and fvg.kind == "bullish":
                if fvg.bottom <= price <= fvg.top:
                    return fvg
            elif sweep["direction"] == "bearish" and fvg.kind == "bearish":
                if fvg.bottom <= price <= fvg.top:
                    return fvg
        return None

    def _next_liquidity_level(self, df: pd.DataFrame, direction: str) -> float:
        window = df.iloc[-self.SWING_LOOKBACK:]
        if direction == "bullish":
            return round(window["high"].max() + 3 * PIP, 5)
        return round(window["low"].min() - 3 * PIP, 5)

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------
    def _score_confidence(
        self,
        conditions: int,
        sweep: dict,
        ob: OrderBlock | None,
        fvg: FVG | None,
    ) -> float:
        base = {1: 0.50, 2: 0.72, 3: 0.88}.get(conditions, 0.50)

        # Bonus: OB is tight (< 10 pips) → cleaner level
        if ob and (ob.ob_high - ob.ob_low) / PIP < 10:
            base += 0.04
        # Bonus: FVG is unmitigated (price not yet fully inside)
        if fvg and (fvg.top - fvg.bottom) / PIP > 3:
            base += 0.04

        return round(min(base, 1.0), 3)

    # ------------------------------------------------------------------
    def _get_df(self, tf: str) -> pd.DataFrame | None:
        if self.data_feed is None:
            return None
        if isinstance(self.data_feed, dict):
            return self.data_feed.get(tf)
        try:
            return self.data_feed.get_ohlcv("EURUSD", tf, bars=200)
        except Exception:
            return None
