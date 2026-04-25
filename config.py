from dotenv import load_dotenv
import os

load_dotenv()

# ---------------------------------------------------------------------------
# Strategy toggle system
# Each strategy checks config.STRATEGIES[name]["enabled"] before running.
# ---------------------------------------------------------------------------
STRATEGIES = {
    "london_breakout": {
        "enabled": True,
        "risk_per_trade": 1.0,   # % of account balance risked per trade
        "session": "london",
        "min_confidence": 0.7,
    },
    "ict_smart_money": {
        "enabled": True,
        "risk_per_trade": 0.8,
        "session": "any",
        "min_confidence": 0.75,
    },
    "asian_ny_range": {
        "enabled": True,
        "risk_per_trade": 1.0,
        "session": "newyork",
        "min_confidence": 0.65,
    },
    "mean_reversion": {
        "enabled": True,
        "risk_per_trade": 0.8,
        "session": "afternoon",
        "min_confidence": 0.65,
    },
}

# ---------------------------------------------------------------------------
# Global risk settings
# ---------------------------------------------------------------------------
RISK = {
    "max_daily_loss_pct": 4.0,   # halt trading if daily drawdown exceeds this
    "max_open_trades": 2,        # maximum concurrent open positions
    "max_weekly_loss_pct": 8.0,  # halt trading for the week if exceeded
    "default_lot_size": 0.01,    # fallback lot size when dynamic sizing is off
    "symbol": "EURUSD",
}

# ---------------------------------------------------------------------------
# MT5 credentials (loaded from .env)
# ---------------------------------------------------------------------------
MT5_LOGIN    = int(os.getenv("MT5_LOGIN") or "0")
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER", "")

# ---------------------------------------------------------------------------
# External API keys
# ---------------------------------------------------------------------------
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# ---------------------------------------------------------------------------
# Database (Neon PostgreSQL)
# ---------------------------------------------------------------------------
NEON_DATABASE_URL = os.getenv("NEON_DATABASE_URL", "")

# ---------------------------------------------------------------------------
# Paper trading balance (used on Linux/Railway when MT5 is unavailable)
# ---------------------------------------------------------------------------
PAPER_BALANCE = float(os.getenv("PAPER_BALANCE", "10000.0"))

# ---------------------------------------------------------------------------
# Session windows (EST hour ranges, inclusive start / exclusive end)
# Based on EST (UTC-5 standard, UTC-4 daylight)
# ---------------------------------------------------------------------------
SESSION_HOURS = {
    "asian":     (20, 24),   # 8pm–midnight EST
    "london":    (7,  9),    # 7am–9am EST (overlap open)
    "newyork":   (8,  12),   # 8am–noon EST
    "afternoon": (12, 17),   # noon–5pm EST
    "dead":      (17, 20),   # 5pm–8pm EST (low liquidity)
    "any":       (0,  24),
}
