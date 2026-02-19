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

    # Crypto symbol suffixes for webhook routing
    CRYPTO_SUFFIXES = ("-USD", "-USDT", "-BTC", "-ETH")

    # Rate limiting: max signals per symbol within a window
    RATE_LIMIT_WINDOW = 60   # seconds
    RATE_LIMIT_MAX = 2       # max signals per symbol per window
    GLOBAL_MIN_INTERVAL = 5  # minimum seconds between ANY webhook call

    def __init__(self, config):
        self.config = config
        self.webhook_url = config.traderspost_webhook_url
        self.webhook_url_secondary = config.traderspost_webhook_url_secondary
        self.webhook_url_crypto = getattr(config, 'traderspost_webhook_url_crypto', '') or ''
        self.api_key = config.traderspost_api_key
        self._connected = bool(self.webhook_url)
        self.signal_history = []
        self.dual_mode = bool(self.webhook_url and self.webhook_url_secondary)
        # Rate limiting state
        self._symbol_signals = {}  # {symbol: [timestamp, ...]}
        self._last_webhook_time = 0
        if self.dual_mode:
            log.info("TradersPost DUAL MODE: signals sent to both live and paper webhooks")
        if self.webhook_url_crypto:
            log.info("TradersPost CRYPTO webhook configured - crypto signals route separately")

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
        symbol = signal.get("symbol", "")

        # Exit signals ALWAYS go through - never rate limit closing positions
        is_exit = signal.get("source") == "exit" or action in ("sell", "cover", "close", "exit")

        # Block short/bearish entry signals (TradersPost strategy is bullish-only)
        if not is_exit and action in ("sell", "short"):
            log.warning(f"BLOCKED: {action} {symbol} - TradersPost is bullish-only, no short entries")
            return {"success": False, "reason": "long_only", "blocked": True}

        # --- Rate Limiting (entries only, NEVER block exits) ---
        now = time.time()
        if not is_exit:
            # Global minimum interval between webhook calls
            since_last = now - self._last_webhook_time
            if since_last < self.GLOBAL_MIN_INTERVAL:
                log.warning(
                    f"RATE LIMIT: Global cooldown - {since_last:.1f}s since last webhook "
                    f"(min {self.GLOBAL_MIN_INTERVAL}s). Blocking {action} {symbol}"
                )
                return {"success": False, "reason": "rate_limited", "status_code": 429}

            # Per-symbol rate limit
            sym_times = self._symbol_signals.get(symbol, [])
            sym_times = [t for t in sym_times if now - t < self.RATE_LIMIT_WINDOW]
            if len(sym_times) >= self.RATE_LIMIT_MAX:
                log.warning(
                    f"RATE LIMIT: {symbol} has {len(sym_times)} signals in last "
                    f"{self.RATE_LIMIT_WINDOW}s (max {self.RATE_LIMIT_MAX}). Blocking."
                )
                return {"success": False, "reason": "rate_limited", "status_code": 429}
            sym_times.append(now)
            self._symbol_signals[symbol] = sym_times
        else:
            log.info(f"EXIT signal for {symbol} - bypassing rate limits")
        self._last_webhook_time = now

        # Route crypto signals to dedicated crypto webhook
        is_crypto = any(symbol.upper().endswith(s) for s in self.CRYPTO_SUFFIXES)
        if is_crypto and self.webhook_url_crypto:
            target_url = self.webhook_url_crypto
            log.info(f"Routing {symbol} to CRYPTO webhook")
        else:
            target_url = self.webhook_url

        # Map actions to TradersPost format
        # TradersPost supports: buy, sell, exit, cancel
        # "buy" opens long, "sell" opens short, "exit" closes any position
        # is_exit already set above for rate limiting bypass

        action_map = {
            "buy": "buy",
            "sell": "sell",
            "short": "sell",
            "cover": "exit",
            "close": "exit",
        }

        tp_action = action_map.get(action, action)
        # CRITICAL: ALL exit/close/sell signals MUST use "exit" action
        # TradersPost is bullish-only — "sell" with "bearish" sentiment gets rejected
        if is_exit and tp_action != "buy":
            tp_action = "exit"

        payload = {
            "ticker": signal.get("symbol", ""),
            "action": tp_action,
        }

        # Sentiment: ONLY add for "buy" entries (bullish)
        # NEVER add sentiment for exits — TradersPost rejects bearish sentiment
        if tp_action == "buy":
            payload["sentiment"] = "bullish"

        # Add optional fields
        if "quantity" in signal:
            payload["quantity"] = signal["quantity"]

        if "price" in signal:
            payload["price"] = signal["price"]

        # Add stop loss / take profit as TradersPost objects
        # TradersPost requires: {"limitPrice": x} for takeProfit, {"stopPrice": x} for stopLoss
        # The strategy config requires takeProfit on every entry signal
        if not is_exit:
            stop_loss = signal.get("stop_loss")
            take_profit = signal.get("take_profit")
            price = signal.get("price", 0)

            if stop_loss:
                payload["stopLoss"] = {"type": "stop", "stopPrice": round(float(stop_loss), 2)}

            if take_profit:
                payload["takeProfit"] = {"type": "limit", "limitPrice": round(float(take_profit), 2)}
            elif price:
                # Default 2% take profit if none provided (TradersPost requires it)
                default_tp = price * 1.02 if tp_action == "buy" else price * 0.98
                payload["takeProfit"] = {"type": "limit", "limitPrice": round(float(default_tp), 2)}

        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = requests.post(
                target_url,
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
                "webhook": "crypto" if (is_crypto and self.webhook_url_crypto) else "primary",
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

            # Also send to secondary webhook (dual mode)
            if self.dual_mode and self.webhook_url_secondary:
                try:
                    requests.post(
                        self.webhook_url_secondary,
                        json=payload,
                        headers=headers,
                        timeout=10
                    )
                    log.debug(f"TradersPost secondary webhook sent: {action.upper()} {payload['ticker']}")
                except Exception as e2:
                    log.debug(f"TradersPost secondary webhook error: {e2}")

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
