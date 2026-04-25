# Deploying the EUR/USD Forex Bot on Railway

## Architecture

Two services share one repo, one Neon DB:

| Service   | Start command                                                                  | Role                   |
|-----------|--------------------------------------------------------------------------------|------------------------|
| bot       | `python main.py`                                                               | Strategy + order loop  |
| dashboard | `streamlit run dashboard/app.py --server.port $PORT --server.address 0.0.0.0` | Streamlit UI           |

The bot runs in **PAPER MODE** on Railway (Linux, no MT5).
All paper trades are logged to Neon DB and visible on the dashboard.

---

## Prerequisites

1. A [Neon](https://neon.tech) PostgreSQL database (free tier is fine).
2. A [Railway](https://railway.app) account.

---

## Step 1 — Push to GitHub

```bash
git add .
git commit -m "Initial forex bot deploy"
git remote add origin https://github.com/YOUR_USERNAME/forex-bot.git
git push -u origin main
```

---

## Step 2 — Create Railway Project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
2. Select your `forex-bot` repo.
3. Railway will auto-detect `Procfile` and create two services.

---

## Step 3 — Set Environment Variables

In Railway → Project → **Variables**, add:

| Variable             | Value                                          |
|----------------------|------------------------------------------------|
| `NEON_DATABASE_URL`  | Your Neon connection string (with `?sslmode=require`) |
| `DASHBOARD_PASSWORD` | A secure password for the dashboard           |
| `PAPER_BALANCE`      | Starting balance for paper trades (e.g. `10000`) |

See `railway_env_guide.txt` for full details.

---

## Step 4 — Configure Services

For the **dashboard service**, override the start command to:
```
streamlit run dashboard/app.py --server.port $PORT --server.address 0.0.0.0
```

For the **bot service**, the start command is just:
```
python main.py
```

---

## Step 5 — Deploy

Click **Deploy** in Railway. Both services will start.

- Bot logs: visible in Railway → bot service → Logs
- Dashboard: visit the Railway-provided URL for the dashboard service

---

## Local Development

### Install dependencies (Mac/Linux)
```bash
pip install -r requirements.txt
```

### Install dependencies (Windows — with MT5)
```bash
pip install -r requirements-windows.txt
```

### Run bot in dry-run mode (3 ticks, no orders)
```bash
python main.py --dry-run --ticks 3
```

### Run dashboard locally
```bash
streamlit run dashboard/app.py
```

### Run integration tests
```bash
python3 test_engine.py
python3 test_strategies.py
```

---

## Paper Mode vs Live Mode

| Environment | MT5 | Mode       | Orders        |
|-------------|-----|------------|---------------|
| Railway / Linux | ✗ | PAPER MODE | Simulated (SL/TP vs yfinance price) |
| Windows + MT5 credentials | ✓ | LIVE MODE  | Real MT5 orders |

In paper mode:
- Trades are logged to Neon DB with `notes = "PAPER TRADE"`
- SL/TP hits are detected by comparing entry price vs current yfinance price each tick
- Full analytics and dashboard work identically to live mode
