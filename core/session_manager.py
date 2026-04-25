"""
core/session_manager.py
Detects the current Forex trading session based on Eastern Time (EST/EDT).
Sessions are defined by EST clock hours regardless of daylight saving — the
broker's market hours shift with DST anyway, so using a fixed EST offset
produces the most intuitive labels for a US-centric calendar.
"""

from __future__ import annotations
from datetime import datetime
import pytz
import config

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------
EST = pytz.timezone("America/New_York")

# ---------------------------------------------------------------------------
# Session definitions
# Each entry: (display_name, start_hour_EST_inclusive, end_hour_EST_exclusive,
#              active_strategies)
# ---------------------------------------------------------------------------
_SESSION_MAP: list[dict] = [
    {
        "name":       "asian",
        "start":      20,
        "end":        24,   # wraps midnight — handled separately
        "label":      "Asian Session",
        "strategies": ["london_breakout"],          # sets up the range
    },
    {
        "name":       "london",
        "start":      7,
        "end":        9,
        "label":      "London Open",
        "strategies": ["london_breakout", "ict_smart_money"],
    },
    {
        "name":       "newyork",
        "start":      8,
        "end":        12,
        "label":      "New York Session",
        "strategies": ["asian_ny_range", "ict_smart_money"],
    },
    {
        "name":       "afternoon",
        "start":      12,
        "end":        17,
        "label":      "Afternoon Session",
        "strategies": ["mean_reversion"],
    },
    {
        "name":       "dead",
        "start":      17,
        "end":        20,
        "label":      "Dead Zone (low liquidity)",
        "strategies": [],
    },
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _now_est() -> datetime:
    return datetime.now(EST)


def _match_session(hour: int) -> dict:
    """Return the session dict that contains `hour` (EST, 0-23)."""
    # Asian session wraps midnight: 20:00–23:59 → hour >= 20
    # and also 00:00–00:59 handled by checking hour < 1 but we
    # treat hour 0 as still-asian via the wrap check below.
    for s in _SESSION_MAP:
        if s["start"] <= hour < s["end"]:
            return s
    # Asian midnight-wrap: hours 0–0 belong to asian (midnight just ticked over)
    # In practice the asian session ends at midnight so hour 0 is the tail.
    # We return dead for any unmatched hour (e.g. 0–6 pre-london).
    return {"name": "dead", "start": 0, "end": 7, "label": "Pre-London (low liquidity)", "strategies": []}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def current_session() -> str:
    """Return the name of the current trading session (EST-based)."""
    return _match_session(_now_est().hour)["name"]


def is_market_open() -> bool:
    """
    Forex market is closed Saturday 17:00 EST → Sunday 17:00 EST.
    Returns False on weekends during that closure window.
    """
    now = _now_est()
    weekday = now.weekday()   # Monday=0 … Sunday=6
    hour    = now.hour

    # Saturday after 5pm → closed
    if weekday == 5 and hour >= 17:
        return False
    # All of Sunday before 5pm → closed
    if weekday == 6 and hour < 17:
        return False

    return True


def get_session_info() -> dict:
    """
    Return a structured dict describing the current session.

    Shape:
    {
        "session":            str,         # e.g. "newyork"
        "label":              str,         # e.g. "New York Session"
        "start_time_est":     str,         # "08:00"
        "end_time_est":       str,         # "12:00"
        "active_strategies":  list[str],
        "market_open":        bool,
        "current_time_est":   str,         # "10:34 EST"
    }
    """
    now  = _now_est()
    sess = _match_session(now.hour)

    return {
        "session":           sess["name"],
        "label":             sess["label"],
        "start_time_est":    f"{sess['start']:02d}:00",
        "end_time_est":      f"{sess['end']:02d}:00",
        "active_strategies": sess["strategies"],
        "market_open":       is_market_open(),
        "current_time_est":  now.strftime("%H:%M EST (%A)"),
    }


def is_session_active(session: str) -> bool:
    """Check whether a specific session name is active right now."""
    if session == "any":
        return True
    return current_session() == session


def session_label() -> str:
    """Short human-readable label for UI display."""
    sess = _match_session(_now_est().hour)
    status = "OPEN" if is_market_open() else "CLOSED"
    return f"{sess['label']} [{status}]"
