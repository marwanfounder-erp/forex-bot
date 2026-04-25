"""
core/news_filter.py
Fetches high-impact economic calendar events from the ForexFactory public
JSON feed (no API key required) and blocks trading around them.

Source: https://nfs.faireconomy.media/ff_calendar_thisweek.json
Times in the feed are Eastern Time (EST/EDT).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
import requests
import pytz

EST = pytz.timezone("America/New_York")

FF_URL        = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
BLACKOUT_MIN  = 120        # minutes before/after a HIGH event to block trades
REQUEST_TIMEOUT = 8        # seconds

# Currencies we care about for EUR/USD
WATCHED_CURRENCIES = {"USD", "EUR"}


class NewsFilter:
    """
    Reads the ForexFactory weekly calendar and provides safe-to-trade signals.
    Results are cached for 15 minutes to avoid hammering the endpoint.
    """

    def __init__(self) -> None:
        self._cache:      list[dict]        = []
        self._cache_time: Optional[datetime] = None
        self._cache_ttl   = timedelta(minutes=15)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_upcoming_events(self, hours_ahead: int = 2) -> list[dict]:
        """
        Return HIGH/MEDIUM-impact USD and EUR events within the next
        `hours_ahead` hours.

        Each event dict:
        {
            "time":     datetime (UTC-aware),
            "currency": str,
            "impact":   "HIGH" | "MEDIUM" | "LOW",
            "event":    str,
        }
        """
        all_events = self._fetch_events()
        now        = datetime.now(timezone.utc)
        cutoff     = now + timedelta(hours=hours_ahead)

        upcoming = []
        for ev in all_events:
            if ev["time"] is None:
                continue
            if ev["currency"] not in WATCHED_CURRENCIES:
                continue
            if now <= ev["time"] <= cutoff:
                upcoming.append(ev)

        return sorted(upcoming, key=lambda e: e["time"])

    def is_safe_to_trade(self) -> bool:
        """
        Returns False if any HIGH-impact USD or EUR event falls within
        the ±BLACKOUT_MIN window around now.
        """
        all_events = self._fetch_events()
        now        = datetime.now(timezone.utc)
        window     = timedelta(minutes=BLACKOUT_MIN)

        for ev in all_events:
            if ev["time"] is None:
                continue
            if ev["currency"] not in WATCHED_CURRENCIES:
                continue
            if ev["impact"] != "HIGH":
                continue
            if abs(now - ev["time"]) <= window:
                return False

        return True

    def get_next_high_impact(self) -> Optional[dict]:
        """
        Return the next HIGH-impact USD/EUR event after now, or None.
        """
        all_events = self._fetch_events()
        now        = datetime.now(timezone.utc)

        future_high = [
            ev for ev in all_events
            if ev["time"] is not None
            and ev["time"] > now
            and ev["currency"] in WATCHED_CURRENCIES
            and ev["impact"] == "HIGH"
        ]

        if not future_high:
            return None

        return min(future_high, key=lambda e: e["time"])

    def status_string(self) -> str:
        """Human-readable status for the dashboard/console."""
        if not self.is_safe_to_trade():
            nxt = self.get_next_high_impact()
            label = nxt["event"] if nxt else "unknown event"
            return f"BLOCKED — HIGH impact news within {BLACKOUT_MIN}min ({label})"

        nxt = self.get_next_high_impact()
        if nxt:
            mins = int((nxt["time"] - datetime.now(timezone.utc)).total_seconds() / 60)
            return f"Safe to trade | Next high-impact: {nxt['event']} in {mins}min"

        return "Safe to trade | No high-impact events this week"

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _fetch_events(self) -> list[dict]:
        """Fetch and cache the ForexFactory weekly JSON."""
        now = datetime.now(timezone.utc)
        if (
            self._cache_time is not None
            and (now - self._cache_time) < self._cache_ttl
        ):
            return self._cache

        try:
            resp = requests.get(FF_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            print(f"[NewsFilter] Failed to fetch calendar: {exc}")
            # Return stale cache rather than blocking all trades on network error
            return self._cache

        parsed = []
        for item in raw:
            parsed.append({
                "time":     self._parse_time(item.get("date", ""), item.get("time", "")),
                "currency": item.get("country", "").upper(),
                "impact":   self._normalise_impact(item.get("impact", "")),
                "event":    item.get("title", ""),
            })

        self._cache      = parsed
        self._cache_time = now
        return parsed

    @staticmethod
    def _parse_time(date_str: str, time_str: str) -> Optional[datetime]:
        """
        Parse ForexFactory date/time into a UTC-aware datetime.

        date_str example : "Apr 05, 2024"
        time_str examples: "8:30am", "All Day", "Tentative", ""
        """
        if not date_str or not time_str or time_str.lower() in ("all day", "tentative", ""):
            if date_str:
                # Treat as midnight EST for the given date
                try:
                    naive = datetime.strptime(date_str.strip(), "%b %d, %Y")
                    return EST.localize(naive).astimezone(timezone.utc)
                except ValueError:
                    return None
            return None

        # Normalise am/pm spacing: "8:30am" → "8:30am"
        time_clean = time_str.strip().lower().replace(" ", "")
        fmt_12h    = "%b %d, %Y %I:%M%p"

        try:
            naive = datetime.strptime(f"{date_str.strip()} {time_clean}", fmt_12h)
        except ValueError:
            # Try without minutes: "8am"
            try:
                naive = datetime.strptime(f"{date_str.strip()} {time_clean}", "%b %d, %Y %I%p")
            except ValueError:
                return None

        return EST.localize(naive).astimezone(timezone.utc)

    @staticmethod
    def _normalise_impact(raw: str) -> str:
        mapping = {"High": "HIGH", "Medium": "MEDIUM", "Low": "LOW"}
        return mapping.get(raw, raw.upper())
