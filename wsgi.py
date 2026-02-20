"""
WSGI entry point for Render / Gunicorn deployment.

This runs the dashboard + trading engine together in one process.
The dashboard serves on Render's $PORT, and the engine runs in a background thread.

IMPORTANT: Gunicorn must run with --workers 1 to avoid duplicate engines.
We use --threads 4 for concurrent request handling instead.
Do NOT use --preload (engine daemon thread won't survive fork).

Render URL: https://your-app.onrender.com
"""
import os
import sys
import time
import threading
import atexit
import logging
import urllib.request

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

# --- Start the trading engine in a background thread ---
_engine_thread = None
_engine_started = threading.Event()


def _start_engine():
    """Start the trading engine in a daemon thread with crash recovery."""
    global _engine_thread
    if _engine_thread is not None and _engine_thread.is_alive():
        return  # Already running

    def _run_engine():
        """Wrapper that catches ALL crashes (including SystemExit) and logs them."""
        try:
            _engine_started.set()
            log.info("Engine thread starting engine.start()...")
            engine.start()
            log.warning("engine.start() returned normally (engine stopped)")
        except BaseException as e:
            # Catch EVERYTHING — SystemExit, KeyboardInterrupt, Exception
            log.error(f"ENGINE THREAD DIED: {type(e).__name__}: {e}", exc_info=True)
            import traceback
            traceback.print_exc()
            # Try to restart after a delay
            time.sleep(10)
            log.info("Attempting engine restart after crash...")
            try:
                engine.running = False  # Reset state
                engine.start()
            except BaseException as e2:
                log.error(f"ENGINE RESTART FAILED: {type(e2).__name__}: {e2}", exc_info=True)

    _engine_thread = threading.Thread(
        target=_run_engine,
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


# --- Self-ping keep-alive (prevents Render from sleeping the service) ---
def _keep_alive():
    """Ping our own /health endpoint every 10 minutes to stay awake."""
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url:
        log.info("RENDER_EXTERNAL_URL not set - keep-alive disabled")
        return
    health_url = f"{base_url}/health"
    log.info(f"Keep-alive pinging {health_url} every 10 minutes")
    while True:
        time.sleep(600)  # 10 minutes
        try:
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                log.debug(f"Keep-alive ping: {resp.status}")
        except Exception as e:
            log.warning(f"Keep-alive ping failed: {e}")


# Start engine immediately (without --preload, this runs in the worker process)
_start_engine()

# Start keep-alive thread on Render
if os.environ.get("RENDER"):
    _keepalive_thread = threading.Thread(
        target=_keep_alive,
        name="KeepAlive",
        daemon=True,
    )
    _keepalive_thread.start()

# Register cleanup
atexit.register(_stop_engine)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
