"""
Performance Dashboard - Web-based monitoring UI.
Shows live P&L, positions, equity curve, and system status.
"""
import json
from datetime import datetime

from flask import Flask, render_template, jsonify
from flask_cors import CORS

from bot.utils.logger import get_logger

log = get_logger("dashboard")


class Dashboard:
    """Web dashboard for monitoring the trading bot."""

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

    def _setup_routes(self):
        @self.app.route("/")
        def index():
            return render_template("dashboard.html")

        @self.app.route("/api/status")
        def status():
            return jsonify(self.engine.get_status())

        @self.app.route("/api/positions")
        def positions():
            positions = self.engine.positions
            result = []
            for symbol, pos in positions.items():
                result.append({
                    "symbol": symbol,
                    "direction": pos.get("direction", "long"),
                    "quantity": pos.get("quantity", 0),
                    "entry_price": pos.get("entry_price", 0),
                    "current_price": pos.get("current_price", pos.get("entry_price", 0)),
                    "pnl_pct": pos.get("unrealized_pnl_pct", 0) * 100,
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

    def start(self):
        host = self.config.dashboard_host
        port = self.config.dashboard_port
        log.info(f"Dashboard starting at http://{host}:{port}")
        self.app.run(host=host, port=port, debug=False, use_reloader=False)
