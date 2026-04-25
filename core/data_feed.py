"""
core/data_feed.py
Data feed wrapper.

On Windows (MT5 available): uses MetaTrader5 for live OHLCV + tick data.
On Linux/Railway (MT5 unavailable): falls back to yfinance (EURUSD=X).
"""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

import config

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

# ── yfinance (Linux fallback) ──────────────────────────────────────────────
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

import logging as _log

# ---------------------------------------------------------------------------
# Timeframe mappings
# ---------------------------------------------------------------------------
_MT5_TIMEFRAMES: dict[str, int] = {}
if MT5_AVAILABLE and mt5:
    _MT5_TIMEFRAMES = {
        "M1":  mt5.TIMEFRAME_M1,
        "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1":  mt5.TIMEFRAME_H1,
        "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,
    }

# yfinance interval strings
_YF_INTERVALS: dict[str, str] = {
    "M1":  "1m",
    "M5":  "5m",
    "M15": "15m",
    "M30": "30m",
    "H1":  "1h",
    "H4":  "1h",    # yfinance has no 4h; we use 1h and caller takes every 4th if needed
    "D1":  "1d",
}

# yfinance period to fetch enough bars
_YF_PERIODS: dict[str, str] = {
    "M1":  "1d",
    "M5":  "5d",
    "M15": "5d",
    "M30": "10d",
    "H1":  "30d",
    "H4":  "60d",
    "D1":  "2y",
}

_OHLCV_COLS = ["time", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# MT5 helpers
# ---------------------------------------------------------------------------
def _ensure_connected() -> None:
    if not MT5_AVAILABLE or mt5 is None:
        return
    if not mt5.initialize(
        login=config.MT5_LOGIN,
        password=config.MT5_PASSWORD,
        server=config.MT5_SERVER,
    ):
        raise ConnectionError(f"MT5 initialisation failed: {mt5.last_error()}")


def _resolve_tf(timeframe: str) -> int:
    tf = _MT5_TIMEFRAMES.get(timeframe.upper())
    if tf is None:
        raise ValueError(
            f"Unknown timeframe '{timeframe}'. "
            f"Valid options: {list(_MT5_TIMEFRAMES.keys())}"
        )
    return tf


def _rates_to_df(rates) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})
    return df[_OHLCV_COLS].copy()


# ---------------------------------------------------------------------------
# Linux / cloud fallback helpers
# ---------------------------------------------------------------------------

# ── Alpha Vantage (OHLCV, free tier, needs ALPHA_VANTAGE_API_KEY env var) ──
_AV_BASE    = "https://www.alphavantage.co/query"
_AV_TF_MAP  = {
    "M1":  ("FX_INTRADAY", "1min"),
    "M5":  ("FX_INTRADAY", "5min"),
    "M15": ("FX_INTRADAY", "15min"),
    "M30": ("FX_INTRADAY", "30min"),
    "H1":  ("FX_INTRADAY", "60min"),
    "H4":  ("FX_INTRADAY", "60min"),   # no 4h in AV; caller uses every 4th
    "D1":  ("FX_DAILY",    None),
}


def _av_ohlcv(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    """Fetch OHLCV from Alpha Vantage. Requires ALPHA_VANTAGE_API_KEY env var."""
    import os
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "ALPHA_VANTAGE_API_KEY not set — add it to Railway Variables. "
            "Get a free key at alphavantage.co/support/#api-key"
        )

    sym = symbol.upper()
    from_cur = sym[:3]
    to_cur   = sym[3:]
    tf_upper = timeframe.upper()
    func, interval = _AV_TF_MAP.get(tf_upper, ("FX_INTRADAY", "60min"))

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

    # Find the time series key (varies by function)
    ts_key = next((k for k in data if "Time Series" in k), None)
    if not ts_key or not data.get(ts_key):
        note = data.get("Note") or data.get("Information") or str(data)[:200]
        raise ValueError(f"Alpha Vantage returned no data: {note}")

    rows = []
    for ts, v in data[ts_key].items():
        rows.append({
            "time":   pd.Timestamp(ts, tz="UTC"),
            "open":   float(v.get("1. open",  v.get("1a. open (USD)", 0))),
            "high":   float(v.get("2. high",  v.get("2a. high (USD)", 0))),
            "low":    float(v.get("3. low",   v.get("3a. low (USD)",  0))),
            "close":  float(v.get("4. close", v.get("4a. close (USD)",0))),
            "volume": float(v.get("5. volume", 0)),
        })

    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return df.tail(bars)[_OHLCV_COLS].reset_index(drop=True)


# ── Frankfurter (current spot price only, no API key needed) ───────────────
def _frankfurter_price(symbol: str) -> float:
    """Return current mid price for e.g. 'EURUSD' via api.frankfurter.app."""
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


# ── yfinance (kept as last resort, often blocked on cloud IPs) ─────────────
def _yf_symbol(symbol: str) -> str:
    if symbol.upper() in ("EURUSD", "GBPUSD", "USDJPY", "USDCHF",
                          "AUDUSD", "NZDUSD", "USDCAD"):
        return symbol.upper() + "=X"
    return symbol


def _yf_ohlcv(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    if not _YF_AVAILABLE:
        raise RuntimeError("yfinance not installed")

    tf_str = _YF_INTERVALS.get(timeframe.upper(), "1h")
    period = _YF_PERIODS.get(timeframe.upper(), "30d")
    yf_sym = _yf_symbol(symbol)

    ticker = yf.Ticker(yf_sym)
    df_raw = ticker.history(period=period, interval=tf_str, auto_adjust=True)

    if df_raw is None or df_raw.empty:
        raise ValueError(f"yfinance returned no data for {yf_sym} {tf_str}")

    df = pd.DataFrame()
    df["time"]   = df_raw.index
    df["open"]   = df_raw["Open"].values
    df["high"]   = df_raw["High"].values
    df["low"]    = df_raw["Low"].values
    df["close"]  = df_raw["Close"].values
    df["volume"] = df_raw["Volume"].values

    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    else:
        df["time"] = df["time"].dt.tz_convert("UTC")

    return df.tail(bars).reset_index(drop=True)[_OHLCV_COLS].copy()


def _cloud_ohlcv(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    """
    Try Alpha Vantage first, then yfinance.
    Logs a single clear warning instead of spamming on every failure.
    """
    import os
    if os.getenv("ALPHA_VANTAGE_API_KEY"):
        return _av_ohlcv(symbol, timeframe, bars)

    # Fall back to yfinance with suppressed noise
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return _yf_ohlcv(symbol, timeframe, bars)
    except Exception as exc:
        raise ValueError(
            f"No OHLCV data available on Railway. "
            f"Add ALPHA_VANTAGE_API_KEY to Railway Variables for live data. "
            f"(yfinance error: {exc})"
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_ohlcv(
    symbol: str,
    timeframe: str,
    bars: int = 500,
) -> pd.DataFrame:
    """
    Fetch the most recent `bars` candles for `symbol` at `timeframe`.

    Uses MT5 on Windows; falls back to yfinance on Linux/Railway.
    Returns pd.DataFrame with columns: time, open, high, low, close, volume.
    """
    if MT5_AVAILABLE and mt5:
        _ensure_connected()
        tf    = _resolve_tf(timeframe)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
        if rates is None or len(rates) == 0:
            raise ValueError(
                f"No OHLCV data returned for {symbol} {timeframe}: {mt5.last_error()}"
            )
        return _rates_to_df(rates)

    # Linux / cloud fallback (Alpha Vantage → yfinance)
    return _cloud_ohlcv(symbol, timeframe, bars)


def get_current_price(symbol: str) -> dict:
    """
    Return the latest bid, ask, and spread for `symbol`.

    Returns {"bid": float, "ask": float, "spread": float, "time": datetime}.
    On Linux, bid == ask (yfinance doesn't separate bid/ask).
    """
    if MT5_AVAILABLE and mt5:
        _ensure_connected()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ValueError(f"No tick data for {symbol}: {mt5.last_error()}")
        return {
            "bid":    tick.bid,
            "ask":    tick.ask,
            "spread": round(tick.ask - tick.bid, 5),
            "time":   pd.Timestamp(tick.time, unit="s", tz="UTC"),
        }

    # Linux / cloud fallback — use Frankfurter for current spot price
    try:
        price = _frankfurter_price(symbol)
    except Exception:
        # Last resort: try yfinance
        if _YF_AVAILABLE:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                yf_sym = _yf_symbol(symbol)
                df = yf.Ticker(yf_sym).history(period="1d", interval="1m", auto_adjust=True)
            if df is not None and not df.empty:
                price = float(df["Close"].iloc[-1])
            else:
                raise ValueError(f"Cannot get current price for {symbol} on Railway")
        else:
            raise

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
    Intended for backtesting / historical analysis.
    """
    if MT5_AVAILABLE and mt5:
        _ensure_connected()
        tf = _resolve_tf(timeframe)
        from_naive = from_time.replace(tzinfo=None) if from_time.tzinfo else from_time
        to_naive   = to_time.replace(tzinfo=None)   if to_time.tzinfo   else to_time
        rates = mt5.copy_rates_range(symbol, tf, from_naive, to_naive)
        if rates is None or len(rates) == 0:
            raise ValueError(
                f"No candle data for {symbol} {timeframe} "
                f"({from_naive} → {to_naive}): {mt5.last_error()}"
            )
        return _rates_to_df(rates)

    # Linux fallback: fetch a large window and filter
    bars_estimate = 2000
    df = _yf_ohlcv(symbol, timeframe, bars_estimate)
    mask = (df["time"] >= pd.Timestamp(from_time, tz="UTC")) & \
           (df["time"] <= pd.Timestamp(to_time,   tz="UTC"))
    return df[mask].reset_index(drop=True)


def get_account_balance() -> float:
    """
    Return current account equity.
    On Linux/paper mode returns a configured default balance.
    """
    if MT5_AVAILABLE and mt5:
        _ensure_connected()
        info = mt5.account_info()
        if info is None:
            raise ConnectionError(f"Cannot fetch account info: {mt5.last_error()}")
        return float(info.equity)

    # Linux / paper mode: return configured paper balance
    return float(config.PAPER_BALANCE)


def get_symbol_info(symbol: str) -> dict:
    """Return point size, digits, and contract size for the symbol."""
    if MT5_AVAILABLE and mt5:
        _ensure_connected()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise ValueError(f"Symbol not found: {symbol}")
        return {
            "point":               info.point,
            "digits":              info.digits,
            "trade_contract_size": info.trade_contract_size,
        }

    # Linux defaults for EURUSD
    return {"point": 0.00001, "digits": 5, "trade_contract_size": 100_000}
