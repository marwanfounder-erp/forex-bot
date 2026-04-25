"""
core/data_feed.py
Market data feed — Alpaca v1beta3 forex REST API (primary)
                   + Frankfurter (fallback for current price).

PRIMARY  : Alpaca forex bars endpoint — real OHLCV candles, free tier,
           works on Linux/Railway. Needs ALPACA_API_KEY + ALPACA_SECRET_KEY.
FALLBACK : Frankfurter — current spot price only, no key needed, always works.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

_OHLCV_COLS = ["time", "open", "high", "low", "close", "volume"]

_ALPACA_DATA_BASE = "https://data.alpaca.markets"

# Alpaca v1beta3 timeframe strings
_TF_MAP: dict[str, str] = {
    "M1":  "1Min",
    "M5":  "5Min",
    "M15": "15Min",
    "M30": "30Min",
    "H1":  "1Hour",
    "H4":  "4Hour",
    "D1":  "1Day",
}

# Minutes per bar — used to compute lookback window
_TF_MINUTES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _alpaca_headers() -> dict:
    """Build Alpaca auth headers from config/env."""
    return {
        "APCA-API-KEY-ID":     config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        "Accept":              "application/json",
    }


def _alpaca_symbol(symbol: str) -> str:
    """Convert 'EURUSD' → 'EUR/USD' for Alpaca."""
    s = symbol.upper()
    if "/" not in s and len(s) == 6:
        return f"{s[:3]}/{s[3:]}"
    return s


def _alpaca_tf(timeframe: str) -> str:
    tf = _TF_MAP.get(timeframe.upper())
    if tf is None:
        raise ValueError(
            f"Unknown timeframe '{timeframe}'. Valid: {list(_TF_MAP.keys())}"
        )
    return tf


def _alpaca_ohlcv(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    """
    Fetch OHLCV bars from Alpaca v1beta3 forex endpoint.
    Requires ALPACA_API_KEY + ALPACA_SECRET_KEY.
    """
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. "
            "Add them to Railway Variables (free at alpaca.markets)."
        )

    alpaca_sym = _alpaca_symbol(symbol)
    tf_str     = _alpaca_tf(timeframe)

    # Build start time wide enough to cover `bars` candles with gaps/weekends
    lookback = bars * _TF_MINUTES.get(timeframe.upper(), 60) * 2
    start    = datetime.now(timezone.utc) - timedelta(minutes=lookback)

    params = {
        "symbols":   alpaca_sym,
        "timeframe": tf_str,
        "start":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit":     min(bars, 10_000),
        "sort":      "asc",
    }

    resp = requests.get(
        f"{_ALPACA_DATA_BASE}/v1beta3/forex/bars",
        headers=_alpaca_headers(),
        params=params,
        timeout=15,
    )
    _raise_for_alpaca(resp)
    data = resp.json()

    bar_list = data.get("bars", {}).get(alpaca_sym, [])
    if not bar_list:
        raise ValueError(
            f"Alpaca returned no bars for {symbol} {timeframe}. "
            "Check that the market is not closed and your API keys are valid."
        )

    rows = [
        {
            "time":   pd.Timestamp(b["t"], tz="UTC"),
            "open":   float(b["o"]),
            "high":   float(b["h"]),
            "low":    float(b["l"]),
            "close":  float(b["c"]),
            "volume": float(b.get("v", 0)),
        }
        for b in bar_list
    ]

    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return df.tail(bars)[_OHLCV_COLS].reset_index(drop=True)


def _alpaca_latest_price(symbol: str) -> float:
    """Return the close of the most recent Alpaca forex bar."""
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        raise RuntimeError("Alpaca API keys not set")

    alpaca_sym = _alpaca_symbol(symbol)
    resp = requests.get(
        f"{_ALPACA_DATA_BASE}/v1beta3/forex/latest/bars",
        headers=_alpaca_headers(),
        params={"symbols": alpaca_sym},
        timeout=10,
    )
    _raise_for_alpaca(resp)
    data = resp.json()
    bar  = data.get("bars", {}).get(alpaca_sym)
    if bar is None:
        raise ValueError(f"Alpaca: no latest bar for {symbol}")
    return float(bar["c"])


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


def _raise_for_alpaca(resp: requests.Response) -> None:
    """Raise a clear error if the Alpaca response is not 2xx."""
    if resp.ok:
        return
    try:
        msg = resp.json().get("message", resp.text[:200])
    except Exception:
        msg = resp.text[:200]
    raise RuntimeError(f"Alpaca API error {resp.status_code}: {msg}")


# ---------------------------------------------------------------------------
# Public API  (signatures unchanged — drop-in replacement)
# ---------------------------------------------------------------------------

def get_ohlcv(
    symbol: str,
    timeframe: str,
    bars: int = 500,
) -> pd.DataFrame:
    """
    Fetch the most recent `bars` candles for `symbol` at `timeframe`.

    Uses Alpaca v1beta3 forex REST API.
    Returns pd.DataFrame with columns: time, open, high, low, close, volume.
    """
    df = _alpaca_ohlcv(symbol, timeframe, bars)
    log.debug("Alpaca: %d %s bars for %s", len(df), timeframe, symbol)
    return df


def get_current_price(symbol: str) -> dict:
    """
    Return the latest price for `symbol`.

    Primary:  Alpaca latest forex bar close.
    Fallback: Frankfurter API (no key, always works).

    Returns {"bid": float, "ask": float, "spread": float, "time": Timestamp}.
    """
    try:
        price = _alpaca_latest_price(symbol)
        log.debug("Alpaca latest bar %s: %.5f", symbol, price)
        return {
            "bid":    price,
            "ask":    price,
            "spread": 0.0,
            "time":   pd.Timestamp.now(tz="UTC"),
        }
    except Exception as exc:
        log.warning("Alpaca price failed (%s) — falling back to Frankfurter", exc)

    price = _frankfurter_price(symbol)
    log.debug("Frankfurter fallback %s: %.5f", symbol, price)
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
    Uses Alpaca v1beta3 forex REST API with explicit start/end.
    """
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")

    alpaca_sym = _alpaca_symbol(symbol)
    tf_str     = _alpaca_tf(timeframe)

    params = {
        "symbols":   alpaca_sym,
        "timeframe": tf_str,
        "start":     from_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end":       to_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit":     10_000,
        "sort":      "asc",
    }

    resp = requests.get(
        f"{_ALPACA_DATA_BASE}/v1beta3/forex/bars",
        headers=_alpaca_headers(),
        params=params,
        timeout=15,
    )
    _raise_for_alpaca(resp)
    data     = resp.json()
    bar_list = data.get("bars", {}).get(alpaca_sym, [])

    rows = [
        {
            "time":   pd.Timestamp(b["t"], tz="UTC"),
            "open":   float(b["o"]),
            "high":   float(b["h"]),
            "low":    float(b["l"]),
            "close":  float(b["c"]),
            "volume": float(b.get("v", 0)),
        }
        for b in bar_list
    ]
    return pd.DataFrame(rows)[_OHLCV_COLS] if rows else pd.DataFrame(columns=_OHLCV_COLS)


def get_account_balance() -> float:
    """Return paper trading balance from config/env."""
    return float(config.PAPER_BALANCE)


def get_symbol_info(symbol: str) -> dict:
    """Return standard EURUSD symbol metadata."""
    return {"point": 0.00001, "digits": 5, "trade_contract_size": 100_000}
