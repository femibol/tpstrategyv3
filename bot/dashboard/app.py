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

        @self.app.route("/api/scanner")
        def scanner():
            return jsonify(self.engine.get_scanner_data())

        @self.app.route("/api/analysis")
        def analysis():
            return jsonify(self.engine.get_analysis_log())

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

        # --- Signal Routing API ---

        @self.app.route("/api/signal", methods=["POST"])
        @self._require_auth
        def submit_signal():
            """
            Submit a manual trading signal.

            POST JSON:
            {
                "symbol": "NVDA",
                "action": "buy",
                "price": 500.00,        (optional - uses market price)
                "quantity": 10,          (optional - auto-sized)
                "stop_loss": 485.00,     (optional - 3% default)
                "take_profit": 530.00,   (optional - 6% default)
                "strategy": "manual",    (optional)
                "asset_type": "stock",   (optional - "stock" or "option")
                "expiry": "20250117",    (optional - for options)
                "strike": 500.0,         (optional - for options)
                "right": "C"             (optional - "C" call or "P" put)
            }
            """
            data = request.get_json()
            if not data:
                return jsonify({"error": "No JSON data"}), 400

            symbol = data.get("symbol", "").upper()
            action = data.get("action", "").lower()

            if not symbol or not action:
                return jsonify({"error": "symbol and action required"}), 400

            if action not in ("buy", "sell", "short", "cover", "close"):
                return jsonify({"error": f"Invalid action: {action}"}), 400

            # Build signal
            signal = {
                "symbol": symbol,
                "action": action,
                "confidence": float(data.get("confidence", 0.7)),
                "source": "manual",
                "strategy": data.get("strategy", "manual"),
                "reason": data.get("reason", f"Manual signal via API"),
            }

            if data.get("price"):
                signal["price"] = float(data["price"])
            else:
                price = self.engine.market_data.get_price(symbol) if self.engine.market_data else None
                if price:
                    signal["price"] = price
                else:
                    return jsonify({"error": f"No price available for {symbol}. Provide 'price' in request."}), 400

            if data.get("quantity"):
                signal["quantity"] = int(data["quantity"])
            if data.get("stop_loss"):
                signal["stop_loss"] = float(data["stop_loss"])
            if data.get("take_profit"):
                signal["take_profit"] = float(data["take_profit"])
            if data.get("asset_type"):
                signal["asset_type"] = data["asset_type"]
            if data.get("expiry"):
                signal["expiry"] = data["expiry"]
            if data.get("strike"):
                signal["strike"] = float(data["strike"])
            if data.get("right"):
                signal["right"] = data["right"]

            results = self.engine.handle_manual_signal(signal)
            log.info(f"Manual signal submitted: {action.upper()} {symbol} | Result: {results}")

            return jsonify({"status": "ok", "results": results})

        # --- Politician Trade APIs ---

        @self.app.route("/api/politicians/status")
        def politician_status():
            if self.engine.politician_tracker:
                return jsonify(self.engine.politician_tracker.get_status())
            return jsonify({"error": "Politician tracker not enabled"}), 404

        @self.app.route("/api/politicians/trades")
        def politician_trades():
            if self.engine.politician_tracker:
                limit = request.args.get("limit", 50, type=int)
                return jsonify(self.engine.politician_tracker.get_recent_disclosures(limit))
            return jsonify([])

        @self.app.route("/api/politicians/signals")
        def politician_signals():
            if self.engine.politician_tracker:
                return jsonify(self.engine.politician_tracker.get_signals())
            return jsonify([])

        @self.app.route("/api/politicians/check", methods=["POST"])
        @self._require_auth
        def politician_check():
            """Manually trigger check for new politician trades."""
            if self.engine.politician_tracker:
                trades = self.engine.politician_tracker.manual_check()
                return jsonify({"status": "checked", "new_trades": len(trades), "trades": trades})
            return jsonify({"error": "Politician tracker not enabled"}), 404

        @self.app.route("/api/politicians/add", methods=["POST"])
        @self._require_auth
        def add_politician():
            """Add a politician to track."""
            data = request.get_json()
            if not data or not data.get("politician_id") or not data.get("name"):
                return jsonify({"error": "politician_id and name required"}), 400

            if self.engine.politician_tracker:
                self.engine.politician_tracker.add_politician(
                    politician_id=data["politician_id"],
                    name=data["name"],
                    chamber=data.get("chamber", "House"),
                    party=data.get("party", ""),
                    priority=data.get("priority", 3),
                    notable=data.get("notable", ""),
                )
                return jsonify({"status": "added", "politician": data["name"]})
            return jsonify({"error": "Politician tracker not enabled"}), 404

        # --- Webhook receiver for TradingView (on same server) ---

        @self.app.route("/webhook/tradingview", methods=["POST"])
        def tradingview_webhook():
            """Receive TradingView webhook on the main dashboard server."""
            if self.engine.tv_receiver:
                # Delegate to TV receiver's route handler
                return self.engine.tv_receiver.app.test_client().post(
                    "/webhook/tradingview",
                    data=request.data,
                    headers=dict(request.headers),
                ).data
            # Handle directly if no separate TV receiver
            data = request.get_json(force=True)
            if not data:
                return jsonify({"error": "No data"}), 400

            signal = {
                "symbol": (data.get("symbol") or data.get("ticker", "")).upper(),
                "action": (data.get("action") or "buy").lower(),
                "price": float(data.get("price", 0)) if data.get("price") else None,
                "confidence": float(data.get("confidence", 0.7)),
                "source": "tradingview_webhook",
                "strategy": "tradingview",
                "reason": f"TradingView alert: {data.get('comment', '')}",
            }
            if data.get("stop_loss"):
                signal["stop_loss"] = float(data["stop_loss"])
            if data.get("take_profit"):
                signal["take_profit"] = float(data["take_profit"])

            self.engine._handle_tv_signal(signal)
            return jsonify({"status": "ok"})

    def start(self):
        host = self.config.dashboard_host
        port = self.config.dashboard_port
        log.info(f"Dashboard starting at http://{host}:{port}")
        self.app.run(host=host, port=port, debug=False, use_reloader=False)
