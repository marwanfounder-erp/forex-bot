"""
strategies/mean_reversion.py

Mean Reversion Strategy
────────────────────────
• Timeframe : H1 candles
• Indicators : Bollinger Bands (20, 2.0), RSI (14), ATR (14), ADX (14)
• BUY  : close < lower band AND RSI < 30
• SELL : close > upper band AND RSI > 70
• Filter : skip when ADX > 25 (trending — bad for mean reversion)
• SL   : entry ± 1.5 × ATR(14)
• TP   : middle Bollinger Band (20-period SMA)
• Confidence : RSI extremity + price/band distance + ATR ratio
"""

from __future__ import annotations
import pandas as pd
import numpy as np

from strategies.base_strategy import BaseStrategy

PIP = 0.0001

# ADX threshold: above this the market is trending, skip mean reversion
ADX_TREND_THRESHOLD = 25.0


class MeanReversion(BaseStrategy):
    name = "mean_reversion"

    BB_PERIOD      = 20
    BB_STD         = 2.0
    RSI_PERIOD     = 14
    ATR_PERIOD     = 14
    ADX_PERIOD     = 14
    RSI_OVERSOLD   = 30.0
    RSI_OVERBOUGHT = 70.0

    # ------------------------------------------------------------------
    def generate_signal(self) -> dict:
        df = self._get_data()
        if df is None or len(df) < self.BB_PERIOD + self.ADX_PERIOD + 5:
            return self._empty_signal("Insufficient H1 data")

        df = self._add_indicators(df)

        # Drop NaN rows produced by indicator warm-up
        df = df.dropna(subset=["bb_upper", "bb_lower", "bb_mid", "rsi", "atr", "adx"])
        if df.empty:
            return self._empty_signal("Indicators still warming up")

        latest = df.iloc[-1]
        close  = latest["close"]

        # ── Trend filter ──────────────────────────────────────────────
        if latest["adx"] > ADX_TREND_THRESHOLD:
            return self._empty_signal(
                f"ADX {latest['adx']:.1f} > {ADX_TREND_THRESHOLD} — trending, skip"
            )

        # ── Bollinger Band / RSI conditions ───────────────────────────
        is_buy  = close < latest["bb_lower"] and latest["rsi"] < self.RSI_OVERSOLD
        is_sell = close > latest["bb_upper"] and latest["rsi"] > self.RSI_OVERBOUGHT

        if not is_buy and not is_sell:
            return self._empty_signal(
                f"No extreme: close={close:.5f} RSI={latest['rsi']:.1f} "
                f"BB({latest['bb_lower']:.5f}–{latest['bb_upper']:.5f})"
            )

        # ── Trade levels ──────────────────────────────────────────────
        atr = latest["atr"]
        tp  = latest["bb_mid"]   # mean reversion target = the middle band

        if is_buy:
            signal = "BUY"
            sl = close - 1.5 * atr
        else:
            signal = "SELL"
            sl = close + 1.5 * atr

        # ── Confidence ────────────────────────────────────────────────
        confidence = self._score_confidence(latest, df, is_buy)

        if confidence < self.min_confidence:
            return self._empty_signal(
                f"Confidence {confidence:.2f} below minimum {self.min_confidence}"
            )

        return {
            "signal":      signal,
            "confidence":  confidence,
            "entry_price": round(close, 5),
            "stop_loss":   round(sl, 5),
            "take_profit": round(tp, 5),
            "reason": (
                f"MeanRev {signal} | RSI={latest['rsi']:.1f} "
                f"ADX={latest['adx']:.1f} ATR={atr/PIP:.1f}pips | "
                f"BB mid={tp:.5f} | RR {self.calculate_rr(close, sl, tp)}"
            ),
        }

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------
    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df["close"]

        # Bollinger Bands
        sma            = close.rolling(self.BB_PERIOD).mean()
        std            = close.rolling(self.BB_PERIOD).std()
        df["bb_mid"]   = sma
        df["bb_upper"] = sma + self.BB_STD * std
        df["bb_lower"] = sma - self.BB_STD * std

        # RSI
        df["rsi"] = self._rsi(close, self.RSI_PERIOD)

        # ATR
        df["atr"] = self._atr(df, self.ATR_PERIOD)

        # ADX
        df["adx"] = self._adx(df, self.ADX_PERIOD)

        return df

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        hl  = df["high"] - df["low"]
        hc  = (df["high"] - df["close"].shift()).abs()
        lc  = (df["low"]  - df["close"].shift()).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def _adx(df: pd.DataFrame, period: int) -> pd.Series:
        """Wilder-smoothed ADX (directional movement index)."""
        up   = df["high"].diff()
        down = -df["low"].diff()

        plus_dm  = up.where((up > down) & (up > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)

        hl   = df["high"] - df["low"]
        hc   = (df["high"] - df["close"].shift()).abs()
        lc   = (df["low"]  - df["close"].shift()).abs()
        tr   = pd.concat([hl, hc, lc], axis=1).max(axis=1)

        atr      = tr.rolling(period).mean()
        plus_di  = 100 * plus_dm.rolling(period).mean()  / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.rolling(period).mean() / atr.replace(0, np.nan)

        dx  = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        adx = dx.rolling(period).mean()
        return adx

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------
    def _score_confidence(
        self, latest: pd.Series, df: pd.DataFrame, is_buy: bool
    ) -> float:
        score = 0.0

        # Factor 1: RSI extremity (0.40 weight)
        # How far beyond 30/70 is the RSI?
        if is_buy:
            rsi_excess = max(0, self.RSI_OVERSOLD  - latest["rsi"])   # e.g. RSI=22 → 8
        else:
            rsi_excess = max(0, latest["rsi"] - self.RSI_OVERBOUGHT)  # e.g. RSI=76 → 6
        score += 0.40 * min(rsi_excess / 15, 1.0)   # 15 points excess = max

        # Factor 2: how far price is beyond the Bollinger Band (0.35 weight)
        if is_buy:
            band_excess = (latest["bb_lower"] - latest["close"]) / PIP
        else:
            band_excess = (latest["close"] - latest["bb_upper"]) / PIP
        score += 0.35 * min(max(band_excess, 0) / 10, 1.0)  # 10 pips beyond = max

        # Factor 3: ATR ratio vs 20-period ATR mean (0.25 weight)
        # Lower ATR relative to recent average = more ranging = better for MR
        avg_atr = df["atr"].tail(20).mean()
        if avg_atr > 0:
            atr_ratio = latest["atr"] / avg_atr
            # ratio < 1 → quieter than average → good
            # ratio > 1 → more volatile → not ideal
            atr_score = max(0, 1.0 - (atr_ratio - 0.8) / 0.8)
            score += 0.25 * min(atr_score, 1.0)
        else:
            score += 0.10

        return round(min(score, 1.0), 3)

    # ------------------------------------------------------------------
    def _get_data(self) -> pd.DataFrame | None:
        if self.data_feed is None:
            return None
        if isinstance(self.data_feed, pd.DataFrame):
            return self.data_feed
        try:
            return self.data_feed.get_ohlcv("EURUSD", "H1", bars=200)
        except Exception:
            return None
