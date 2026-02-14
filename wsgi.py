"""
WSGI entry point for Render / Gunicorn deployment.

This runs the dashboard + trading engine together in one process.
The dashboard serves on Render's $PORT, and the engine runs in a background thread.

Render URL: https://your-app.onrender.com
"""
import os
import sys
import threading

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.config import Config
from bot.engine import TradingEngine
from bot.dashboard.app import Dashboard

# Initialize
config = Config()

# Override port from Render's PORT env var
render_port = os.environ.get("PORT")
if render_port:
    os.environ["DASHBOARD_PORT"] = render_port

# Create engine
engine = TradingEngine(config)

# Create dashboard (this gives us the Flask app for gunicorn)
dashboard = Dashboard(engine, config)
app = dashboard.app

# Start the trading engine in a background thread when the first request comes in
_engine_started = False


@app.before_request
def start_engine_once():
    """Start the trading engine on first request (lazy init)."""
    global _engine_started
    if not _engine_started:
        _engine_started = True
        engine_thread = threading.Thread(target=engine.start, daemon=True)
        engine_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
