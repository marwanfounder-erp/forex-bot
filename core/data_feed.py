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
# yfinance helpers
# ---------------------------------------------------------------------------
def _yf_symbol(symbol: str) -> str:
    """Convert MT5-style 'EURUSD' → yfinance 'EURUSD=X'."""
    if symbol.upper() in ("EURUSD", "GBPUSD", "USDJPY", "USDCHF",
                          "AUDUSD", "NZDUSD", "USDCAD"):
        return symbol.upper() + "=X"
    return symbol


def _yf_ohlcv(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    if not _YF_AVAILABLE:
        raise RuntimeError("yfinance is not installed — cannot fetch data on Linux")

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

    # Ensure time is timezone-aware UTC
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    else:
        df["time"] = df["time"].dt.tz_convert("UTC")

    df = df.tail(bars).reset_index(drop=True)
    return df[_OHLCV_COLS].copy()


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

    # Linux / yfinance fallback
    return _yf_ohlcv(symbol, timeframe, bars)


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

    # Linux / yfinance fallback
    if not _YF_AVAILABLE:
        raise RuntimeError("yfinance not installed")

    yf_sym = _yf_symbol(symbol)
    ticker = yf.Ticker(yf_sym)
    df = ticker.history(period="1d", interval="1m", auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"yfinance returned no tick data for {yf_sym}")

    price = float(df["Close"].iloc[-1])
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
