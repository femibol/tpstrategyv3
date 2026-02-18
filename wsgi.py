"""
WSGI entry point for Render / Gunicorn deployment.

This runs the dashboard + trading engine together in one process.
The dashboard serves on Render's $PORT, and the engine runs in a background thread.

IMPORTANT: Gunicorn must run with --workers 1 to avoid duplicate engines.
We use --threads 4 for concurrent request handling instead.

Render URL: https://your-app.onrender.com
"""
import os
import sys
import threading
import atexit
import logging

log = logging.getLogger("trading_bot.wsgi")

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

# --- Start the trading engine immediately in a background thread ---
# This ensures the engine is running as soon as gunicorn loads the app,
# not waiting for the first HTTP request (which may be a health check).
_engine_thread = None


def _start_engine():
    """Start the trading engine in a daemon thread."""
    global _engine_thread
    if _engine_thread is not None and _engine_thread.is_alive():
        return  # Already running
    _engine_thread = threading.Thread(
        target=engine.start,
        name="TradingEngine",
        daemon=True,
    )
    _engine_thread.start()
    log.info("Trading engine started in background thread")


def _stop_engine():
    """Gracefully stop the engine on shutdown."""
    try:
        engine.stop()
    except Exception:
        pass


# Start engine now (at import time / gunicorn preload)
_start_engine()

# Register cleanup
atexit.register(_stop_engine)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
