"""
TradingView Alert Webhook Receiver

Receives alerts from TradingView and converts them to trading signals.

Setup in TradingView:
1. Create an alert on your indicator/strategy
2. Set webhook URL to: http://YOUR_SERVER:5000/webhook/tradingview
3. Set alert message format (JSON):
   {
     "symbol": "{{ticker}}",
     "action": "buy",
     "price": {{close}},
     "interval": "{{interval}}",
     "exchange": "{{exchange}}",
     "time": "{{time}}"
   }
4. Set your webhook secret in .env TRADINGVIEW_WEBHOOK_SECRET
"""
import json
import hmac
import hashlib
from datetime import datetime

from flask import Flask, request, jsonify

from bot.utils.logger import get_logger

log = get_logger("signals.tradingview")


class TradingViewReceiver:
    """
    Flask server that receives TradingView webhook alerts.

    Runs on a separate port, validates incoming signals,
    and passes them to the trading engine callback.
    """

    def __init__(self, config, callback=None):
        self.config = config
        self.callback = callback
        self.secret = config.tradingview_webhook_secret
        self.app = Flask("tradingview_receiver")
        self.received_signals = []
        self._setup_routes()

    def _setup_routes(self):
        """Set up Flask routes."""

        @self.app.route("/webhook/tradingview", methods=["POST"])
        def receive_webhook():
            """Receive and process TradingView webhook."""
            try:
                # Validate secret
                if self.secret:
                    auth = request.headers.get("X-Webhook-Secret", "")
                    # Also check query param
                    if not auth:
                        auth = request.args.get("secret", "")
                    # Also check in body
                    body_secret = None
                    try:
                        data = request.get_json(force=True)
                        body_secret = data.get("secret", "")
                    except Exception:
                        data = None

                    if auth != self.secret and body_secret != self.secret:
                        log.warning("TradingView webhook: invalid secret")
                        return jsonify({"error": "Unauthorized"}), 401

                # Parse signal
                if data is None:
                    data = request.get_json(force=True)

                if not data:
                    return jsonify({"error": "No data"}), 400

                signal = self._parse_signal(data)
                if signal is None:
                    return jsonify({"error": "Invalid signal format"}), 400

                log.info(
                    f"TradingView signal: {signal['action'].upper()} "
                    f"{signal['symbol']} @ ${signal.get('price', 'N/A')}"
                )

                self.received_signals.append({
                    "time": datetime.now().isoformat(),
                    "signal": signal,
                    "raw": data,
                })

                # Pass to engine callback
                if self.callback:
                    self.callback(signal)

                return jsonify({"status": "ok", "signal": signal}), 200

            except Exception as e:
                log.error(f"Webhook error: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/health", methods=["GET"])
        def health():
            return jsonify({
                "status": "ok",
                "signals_received": len(self.received_signals),
                "uptime": datetime.now().isoformat(),
            })

    def _parse_signal(self, data):
        """Parse TradingView webhook data into a trading signal."""
        # Required fields
        symbol = data.get("symbol") or data.get("ticker")
        action = data.get("action") or data.get("strategy", {}).get("order_action")

        if not symbol or not action:
            log.warning(f"Missing required fields: symbol={symbol}, action={action}")
            return None

        # Clean symbol (remove exchange prefix if present)
        if ":" in symbol:
            symbol = symbol.split(":")[-1]

        # Normalize action
        action = action.lower().strip()
        valid_actions = {"buy", "sell", "short", "cover", "close", "long"}
        if action not in valid_actions:
            log.warning(f"Invalid action: {action}")
            return None

        # Map aliases
        if action == "long":
            action = "buy"
        elif action == "close":
            action = "sell"

        signal = {
            "symbol": symbol.upper(),
            "action": action,
            "price": float(data.get("price", 0)) if data.get("price") else None,
            "confidence": float(data.get("confidence", 0.7)),
            "reason": f"TradingView alert: {data.get('comment', action)}",
            "source": "tradingview",
            "received_at": datetime.now().isoformat(),
        }

        # Optional fields
        if data.get("stop_loss") or data.get("stoploss"):
            signal["stop_loss"] = float(data.get("stop_loss") or data.get("stoploss"))

        if data.get("take_profit") or data.get("tp"):
            signal["take_profit"] = float(data.get("take_profit") or data.get("tp"))

        if data.get("quantity") or data.get("qty"):
            signal["quantity"] = int(data.get("quantity") or data.get("qty"))

        return signal

    def start(self, host=None, port=None):
        """Start the webhook server."""
        host = host or self.config.dashboard_host
        port = port or (self.config.dashboard_port + 1)  # Use port+1 for webhooks

        log.info(f"TradingView webhook server starting on {host}:{port}")
        log.info(f"Webhook URL: http://{host}:{port}/webhook/tradingview")

        self.app.run(
            host=host,
            port=port,
            debug=False,
            use_reloader=False,
        )
