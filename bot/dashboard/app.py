"""
Performance Dashboard - Web-based monitoring UI.
Shows live P&L, positions, equity curve, and system status.
Mobile-responsive with touch controls for phone access via Render.
"""
import os
import json
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS

from bot.utils.logger import get_logger

log = get_logger("dashboard")


class Dashboard:
    """Web dashboard for monitoring the trading bot - mobile ready."""

    def __init__(self, engine, config):
        self.engine = engine
        self.config = config
        self.app = Flask(
            "trading_dashboard",
            template_folder=str(
                __import__("pathlib").Path(__file__).parent / "templates"
            )
        )
        CORS(self.app)
        self._setup_routes()

    def _require_auth(self, f):
        """Simple API key auth for mobile control endpoints."""
        @wraps(f)
        def decorated(*args, **kwargs):
            secret = os.environ.get("DASHBOARD_SECRET_KEY", "")
            if secret:
                provided = request.headers.get("X-API-Key", "") or request.args.get("key", "")
                if provided != secret:
                    return jsonify({"error": "Unauthorized"}), 401
            return f(*args, **kwargs)
        return decorated

    def _setup_routes(self):

        @self.app.route("/")
        def index():
            return render_template("dashboard.html")

        @self.app.route("/health")
        def health():
            return jsonify({
                "status": "ok",
                "mode": self.config.mode,
                "running": self.engine.running,
                "uptime": datetime.now().isoformat(),
            })

        # --- Read-only APIs ---

        @self.app.route("/api/status")
        def status():
            return jsonify(self.engine.get_status())

        @self.app.route("/api/positions")
        def positions():
            positions = self.engine.positions
            result = []
            for symbol, pos in positions.items():
                entry = pos.get("entry_price", 0)
                current = pos.get("current_price", entry)
                qty = pos.get("quantity", 0)
                direction = pos.get("direction", "long")
                if direction == "long":
                    pnl_dollars = (current - entry) * qty
                else:
                    pnl_dollars = (entry - current) * qty
                result.append({
                    "symbol": symbol,
                    "direction": direction,
                    "quantity": qty,
                    "entry_price": entry,
                    "current_price": current,
                    "pnl_pct": pos.get("unrealized_pnl_pct", 0) * 100,
                    "pnl_dollars": round(pnl_dollars, 2),
                    "strategy": pos.get("strategy", "unknown"),
                    "stop_loss": pos.get("stop_loss", 0),
                    "take_profit": pos.get("take_profit", 0),
                })
            return jsonify(result)

        @self.app.route("/api/trades")
        def trades():
            return jsonify(self.engine.trade_history[-50:])

        @self.app.route("/api/equity")
        def equity():
            return jsonify(self.engine.equity_curve[-500:])

        @self.app.route("/api/daily")
        def daily():
            return jsonify(self.engine.daily_stats[-30:])

        @self.app.route("/api/notifications")
        def notifications():
            if self.engine.notifier:
                return jsonify(self.engine.notifier.history[-20:])
            return jsonify([])

        # --- Control APIs (for mobile) ---

        @self.app.route("/api/control/pause", methods=["POST"])
        @self._require_auth
        def pause():
            self.engine.paused = True
            log.info("Bot PAUSED via dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("Bot paused via mobile", level="warning")
            return jsonify({"status": "paused"})

        @self.app.route("/api/control/resume", methods=["POST"])
        @self._require_auth
        def resume():
            self.engine.paused = False
            log.info("Bot RESUMED via dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("Bot resumed via mobile", level="success")
            return jsonify({"status": "running"})

        @self.app.route("/api/control/close/<symbol>", methods=["POST"])
        @self._require_auth
        def close_position(symbol):
            symbol = symbol.upper()
            if symbol in self.engine.positions:
                self.engine._close_position(symbol, "manual", "Closed via mobile dashboard")
                return jsonify({"status": "closed", "symbol": symbol})
            return jsonify({"error": f"No position in {symbol}"}), 404

        @self.app.route("/api/control/close-all", methods=["POST"])
        @self._require_auth
        def close_all():
            count = len(self.engine.positions)
            self.engine._close_all_positions("Manual close-all via mobile dashboard")
            return jsonify({"status": "closed_all", "count": count})

        @self.app.route("/api/control/emergency-stop", methods=["POST"])
        @self._require_auth
        def emergency_stop():
            self.engine._close_all_positions("EMERGENCY STOP via mobile")
            self.engine.running = False
            log.critical("EMERGENCY STOP triggered via mobile dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("EMERGENCY STOP via mobile", level="error")
            return jsonify({"status": "stopped", "positions_closed": True})

    def start(self):
        host = self.config.dashboard_host
        port = self.config.dashboard_port
        log.info(f"Dashboard starting at http://{host}:{port}")
        self.app.run(host=host, port=port, debug=False, use_reloader=False)
