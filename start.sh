#!/bin/bash
set -e

echo "Starting EUR/USD Forex Bot..."
python main.py &
BOT_PID=$!
echo "Bot started with PID $BOT_PID"

echo "Starting Streamlit Dashboard..."
exec streamlit run dashboard/app.py \
  --server.port ${PORT:-8501} \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false
