"""
dashboard/health.py
Lightweight Flask health-check endpoint for Railway liveness probes.

Run standalone:  python dashboard/health.py
Or via Procfile: gunicorn dashboard.health:app
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/health")
def health():
    """Railway calls this to confirm the service is alive."""
    return jsonify({
        "status": "ok",
        "service": "eurusd-forex-bot",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 200


@app.route("/")
def index():
    return jsonify({"message": "EUR/USD Forex Bot health service"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
