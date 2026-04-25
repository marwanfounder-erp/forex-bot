#!/bin/bash

# Start the bot in the background
python main.py &

# Start the Streamlit dashboard in the foreground
# Railway needs one process in foreground to stay alive
streamlit run dashboard/app.py \
  --server.port $PORT \
  --server.address 0.0.0.0 \
  --server.headless true
