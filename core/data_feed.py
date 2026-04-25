"""
core/data_feed.py
Market data feed — cloud-native, no MT5.

PRIMARY  : Alpha Vantage (OHLCV candles) via ALPHA_VANTAGE_API_KEY
FALLBACK : Frankfurter API (current price only, no key needed, always works)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

_AV_BASE   = "https://www.alphavantage.co/query"
_AV_TF_MAP = {
    "M1":  ("FX_INTRADAY", "1min"),
    "M5":  ("FX_INTRADAY", "5min"),
    "M15": ("FX_INTRADAY", "15min"),
    "M30": ("FX_INTRADAY", "30min"),
    "H1":  ("FX_INTRADAY", "60min"),
    "H4":  ("FX_INTRADAY", "60min"),   # no 4h in AV; caller uses every 4th row if needed
    "D1":  ("FX_DAILY",    None),
}

_OHLCV_COLS = ["time", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _av_ohlcv(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    """Fetch OHLCV candles from Alpha Vantage. Requires ALPHA_VANTAGE_API_KEY."""
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "ALPHA_VANTAGE_API_KEY not set — add it to Railway Variables. "
            "Get a free key at alphavantage.co/support/#api-key"
        )

    sym      = symbol.upper()
    from_cur = sym[:3]
    to_cur   = sym[3:]
    func, interval = _AV_TF_MAP.get(timeframe.upper(), ("FX_INTRADAY", "60min"))

    params: dict = {
        "function":    func,
        "from_symbol": from_cur,
        "to_symbol":   to_cur,
        "outputsize":  "full",
        "apikey":      api_key,
    }
    if interval:
        params["interval"] = interval

    resp = requests.get(_AV_BASE, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    ts_key = next((k for k in data if "Time Series" in k), None)
    if not ts_key or not data.get(ts_key):
        note = data.get("Note") or data.get("Information") or str(data)[:200]
        raise ValueError(f"Alpha Vantage returned no data: {note}")

    rows = []
    for ts, v in data[ts_key].items():
        rows.append({
            "time":   pd.Timestamp(ts, tz="UTC"),
            "open":   float(v.get("1. open",  v.get("1a. open (USD)",  0))),
            "high":   float(v.get("2. high",  v.get("2a. high (USD)",  0))),
            "low":    float(v.get("3. low",   v.get("3a. low (USD)",   0))),
            "close":  float(v.get("4. close", v.get("4a. close (USD)", 0))),
            "volume": float(v.get("5. volume", 0)),
        })

    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return df.tail(bars)[_OHLCV_COLS].reset_index(drop=True)


def _frankfurter_price(symbol: str) -> float:
    """Return current mid price via api.frankfurter.app. No API key needed."""
    sym      = symbol.upper()
    from_cur = sym[:3]
    to_cur   = sym[3:]
    resp = requests.get(
        f"https://api.frankfurter.app/latest?from={from_cur}&to={to_cur}",
        timeout=8,
    )
    resp.raise_for_status()
    rate = resp.json()["rates"].get(to_cur)
    if rate is None:
        raise ValueError(f"Frankfurter: no rate for {symbol}")
    return float(rate)


# ---------------------------------------------------------------------------
# Public API  (signatures unchanged — drop-in replacement for old MT5 feed)
# ---------------------------------------------------------------------------

def get_ohlcv(
    symbol: str,
    timeframe: str,
    bars: int = 500,
) -> pd.DataFrame:
    """
    Fetch the most recent `bars` candles for `symbol` at `timeframe`.

    Uses Alpha Vantage (requires ALPHA_VANTAGE_API_KEY).
    Returns pd.DataFrame with columns: time, open, high, low, close, volume.
    """
    df = _av_ohlcv(symbol, timeframe, bars)
    log.debug("Alpha Vantage: %d %s candles for %s", len(df), timeframe, symbol)
    return df


def get_current_price(symbol: str) -> dict:
    """
    Return the latest bid, ask, and spread for `symbol`.

    Primary: Frankfurter (free, always works, no key).
    Fallback: Alpha Vantage last close from M5 candle.

    Returns {"bid": float, "ask": float, "spread": float, "time": Timestamp}.
    """
    try:
        price = _frankfurter_price(symbol)
        log.debug("Frankfurter price %s: %.5f", symbol, price)
        return {
            "bid":    price,
            "ask":    price,
            "spread": 0.0,
            "time":   pd.Timestamp.now(tz="UTC"),
        }
    except Exception as exc:
        log.warning("Frankfurter failed (%s) — falling back to Alpha Vantage", exc)

    # Fallback: last close from Alpha Vantage
    df    = _av_ohlcv(symbol, "M5", 1)
    price = float(df["close"].iloc[-1])
    return {
        "bid":    price,
        "ask":    price,
        "spread": 0.0,
        "time":   pd.Timestamp.now(tz="UTC"),
    }


def get_candles_range(
    symbol: str,
    timeframe: str,
    from_time: datetime,
    to_time: datetime,
) -> pd.DataFrame:
    """
    Fetch candles between from_time and to_time (timezone-aware datetimes).
    Uses Alpha Vantage full output and filters by time range.
    """
    df   = _av_ohlcv(symbol, timeframe, 2000)
    mask = (df["time"] >= pd.Timestamp(from_time, tz="UTC")) & \
           (df["time"] <= pd.Timestamp(to_time,   tz="UTC"))
    return df[mask].reset_index(drop=True)


def get_account_balance() -> float:
    """Return paper trading balance from config/env."""
    return float(config.PAPER_BALANCE)


def get_symbol_info(symbol: str) -> dict:
    """Return standard EURUSD symbol metadata."""
    return {"point": 0.00001, "digits": 5, "trade_contract_size": 100_000}
