"""
TradersPost Integration
- Sends trade signals via webhook to TradersPost
- TradersPost can then execute on connected brokers
- Also receives signals from TradersPost strategies

TradersPost Flow:
TradingView Alert -> TradersPost -> Broker (IBKR/TD/etc)
OR
Bot Signal -> TradersPost Webhook -> Broker
"""
import json
import time
from datetime import datetime

import requests

from bot.brokers.base import BaseBroker
from bot.utils.logger import get_logger

log = get_logger("broker.traderspost")


class TradersPostBroker(BaseBroker):
    """
    TradersPost webhook integration.

    Sends signals to TradersPost which executes them
    on your connected broker account.

    Webhook JSON format:
    {
        "ticker": "AAPL",
        "action": "buy",
        "sentiment": "bullish",
        "quantity": 10,
        "price": 150.00
    }
    """

    def __init__(self, config):
        self.config = config
        self.webhook_url = config.traderspost_webhook_url
        self.api_key = config.traderspost_api_key
        self._connected = bool(self.webhook_url)
        self.signal_history = []

    def connect(self):
        """Validate webhook URL is configured."""
        if self.webhook_url:
            log.info("TradersPost webhook configured")
            self._connected = True
            return True
        log.warning("TradersPost webhook URL not configured")
        self._connected = False
        return False

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def reconnect(self):
        return self.connect()

    def send_signal(self, signal):
        """
        Send a trading signal to TradersPost webhook.

        Args:
            signal: Dict with symbol, action, quantity, price, etc.
        """
        if not self.webhook_url:
            log.warning("TradersPost webhook not configured")
            return None

        action = signal.get("action", "").lower()

        # Map actions to TradersPost format
        action_map = {
            "buy": "buy",
            "sell": "sell",
            "short": "sell",
            "cover": "buy",
        }

        sentiment_map = {
            "buy": "bullish",
            "sell": "bearish",
            "short": "bearish",
            "cover": "bullish",
        }

        payload = {
            "ticker": signal.get("symbol", ""),
            "action": action_map.get(action, action),
            "sentiment": sentiment_map.get(action, "flat"),
        }

        # Add optional fields
        if "quantity" in signal:
            payload["quantity"] = signal["quantity"]

        if "price" in signal:
            payload["price"] = signal["price"]

        # Add signal metadata
        if signal.get("stop_loss"):
            payload["stopLoss"] = signal["stop_loss"]

        if signal.get("take_profit"):
            payload["takeProfit"] = signal["take_profit"]

        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = requests.post(
                self.webhook_url,
                json=payload,
                headers=headers,
                timeout=10
            )

            success = response.status_code in (200, 201, 202)

            result = {
                "success": success,
                "status_code": response.status_code,
                "response": response.text[:200],
                "payload": payload,
                "time": datetime.now().isoformat(),
            }

            self.signal_history.append(result)

            if success:
                log.info(
                    f"TradersPost signal sent: {action.upper()} "
                    f"{payload['ticker']} | Response: {response.status_code}"
                )
            else:
                log.error(
                    f"TradersPost signal failed: {response.status_code} "
                    f"| {response.text[:100]}"
                )

            return result

        except requests.exceptions.Timeout:
            log.error("TradersPost webhook timeout")
            return None
        except Exception as e:
            log.error(f"TradersPost error: {e}")
            return None

    def place_order(self, symbol, action, quantity, order_type="LIMIT",
                    limit_price=None, stop_price=None):
        """Place order via TradersPost webhook."""
        signal = {
            "symbol": symbol,
            "action": action.lower(),
            "quantity": quantity,
            "price": limit_price or stop_price,
        }
        result = self.send_signal(signal)
        if result and result.get("success"):
            return {
                "order_id": f"tp_{int(time.time())}",
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "status": "sent",
            }
        return None

    def cancel_order(self, order_id):
        """TradersPost doesn't support direct order cancellation via webhook."""
        log.warning("TradersPost: order cancellation not supported via webhook")
        return False

    def get_positions(self):
        """TradersPost doesn't expose positions via webhook."""
        return {}

    def get_account_summary(self):
        """TradersPost doesn't expose account data via webhook."""
        return None

    def get_order_status(self, order_id):
        """TradersPost doesn't expose order status via webhook."""
        return None
