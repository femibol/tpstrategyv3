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
from pathlib import Path

import requests

from bot.brokers.base import BaseBroker
from bot.utils.logger import get_logger

log = get_logger("broker.traderspost")

# Persistent signal log — survives restarts, tracks every signal with strategy
_SIGNAL_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_SIGNAL_LOG_FILE = _SIGNAL_LOG_DIR / "signal_log.json"


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
    RATE_LIMIT_MAX = 3       # max signals per symbol per window (was 2)
    GLOBAL_MIN_INTERVAL = 3  # minimum seconds between ANY webhook call (was 5)

    def __init__(self, config):
        self.config = config
        self.webhook_url = config.traderspost_webhook_url
        self.webhook_url_secondary = config.traderspost_webhook_url_secondary
        self.webhook_url_crypto = getattr(config, 'traderspost_webhook_url_crypto', '') or ''
        self.api_key = config.traderspost_api_key
        self.webhook_password = getattr(config, 'traderspost_webhook_password', '') or ''
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

    def _persist_signal(self, signal, result, payload):
        """Save every signal to disk for analysis (survives restarts)."""
        try:
            _SIGNAL_LOG_DIR.mkdir(exist_ok=True)
            existing = []
            if _SIGNAL_LOG_FILE.exists():
                try:
                    with open(_SIGNAL_LOG_FILE, "r") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, Exception):
                    existing = []

            entry = {
                "time": datetime.now().isoformat(),
                "symbol": signal.get("symbol", ""),
                "action": signal.get("action", ""),
                "strategy": signal.get("strategy", signal.get("source", "unknown")),
                "price": signal.get("price", 0),
                "quantity": signal.get("quantity", 0),
                "stop_loss": signal.get("stop_loss", 0),
                "take_profit": signal.get("take_profit", 0),
                "confidence": signal.get("confidence", 0),
                "reason": signal.get("reason", ""),
                "success": result.get("success", False) if result else False,
                "rejected": result.get("rejected", False) if result else False,
                "status_code": result.get("status_code", 0) if result else 0,
                "tp_action": payload.get("action", "") if payload else "",
            }

            existing.append(entry)
            # Keep last 2000 entries
            if len(existing) > 2000:
                existing = existing[-2000:]

            with open(_SIGNAL_LOG_FILE, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            log.debug(f"Could not persist signal: {e}")

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

        # Add webhook password if configured (TradersPost "Invalid Password" fix)
        if self.webhook_password:
            payload["password"] = self.webhook_password

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

            http_ok = response.status_code in (200, 201, 202)

            # Check response body for rejection — TradersPost can return
            # HTTP 200 but still reject the signal (no matching position, etc.)
            resp_text = response.text[:500]
            resp_lower = resp_text.lower()
            rejected = "rejected" in resp_lower or "no open position" in resp_lower
            success = http_ok and not rejected

            result = {
                "success": success,
                "rejected": rejected,
                "status_code": response.status_code,
                "response": resp_text[:200],
                "payload": payload,
                "strategy": signal.get("strategy", signal.get("source", "unknown")),
                "webhook": "crypto" if (is_crypto and self.webhook_url_crypto) else "primary",
                "time": datetime.now().isoformat(),
            }

            self.signal_history.append(result)

            # Persist every signal to disk for post-session analysis
            self._persist_signal(signal, result, payload)

            if success:
                source_strategy = signal.get("strategy", signal.get("source", "unknown"))
                price = signal.get("price", 0)
                qty = signal.get("quantity", 0)
                sl = signal.get("stop_loss", 0)
                tp = signal.get("take_profit", 0)
                total = price * qty if price and qty else 0
                risk_amt = abs(price - sl) * qty if price and sl and qty else 0
                reward_amt = abs(tp - price) * qty if price and tp and qty else 0
                rr = round(reward_amt / risk_amt, 1) if risk_amt > 0 else 0
                log.info(
                    f"TradersPost signal sent: {action.upper()} "
                    f"{payload['ticker']} | Strategy: {source_strategy} | "
                    f"Qty: {qty} @ ${price:.2f} = ${total:,.2f} | "
                    f"Risk: ${risk_amt:,.2f} | Reward: ${reward_amt:,.2f} | "
                    f"R:R {rr}:1 | Response: {response.status_code}"
                )
            elif rejected:
                log.warning(
                    f"TradersPost REJECTED {action.upper()} {payload['ticker']} "
                    f"(HTTP {response.status_code}) | {resp_text[:100]}"
                )
            else:
                log.error(
                    f"TradersPost signal failed: {response.status_code} "
                    f"| {resp_text[:100]}"
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

    def notify_trade(self, signal):
        """
        Mirror a trade to TradersPost for dashboard visibility.
        Called after IBKR executes — TradersPost gets the signal so the trade
        appears in its UI, but IBKR is the actual execution broker.

        NOTE: If your TradersPost strategy has auto-execute ON, this will
        create a duplicate order on the connected broker (e.g. Alpaca).
        To avoid duplicates, either:
        - Set the strategy to paper/log-only mode in TradersPost
        - Use a separate webhook URL for notifications (secondary webhook)
        """
        if not self.webhook_url:
            return None

        symbol = signal.get("symbol", "")
        action = signal.get("action", "").lower()
        is_exit = action in ("sell", "cover", "close", "exit") or signal.get("source") == "exit"

        # Build minimal payload (no SL/TP — IBKR manages those server-side)
        tp_action = "exit" if is_exit else "buy"
        payload = {
            "ticker": symbol,
            "action": tp_action,
        }
        if self.webhook_password:
            payload["password"] = self.webhook_password
        if tp_action == "buy":
            payload["sentiment"] = "bullish"
        if "quantity" in signal:
            payload["quantity"] = signal["quantity"]
        if "price" in signal:
            payload["price"] = signal["price"]

        # Mirror to primary webhook so trades appear in TradersPost dashboard.
        # Secondary webhook is for dual-mode live signal execution, not mirroring.
        target_url = self.webhook_url
        target_label = "primary"

        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = requests.post(
                target_url,
                json=payload,
                headers=headers,
                timeout=10,
            )

            success = response.status_code in (200, 201, 202)
            result = {
                "success": success,
                "status_code": response.status_code,
                "response": response.text[:200],
                "mirror": True,
            }

            # Persist for signal log
            self._persist_signal(
                {**signal, "mirror": True}, result, payload
            )

            log.info(
                f"TP MIRROR: {tp_action.upper()} {symbol} "
                f"qty={signal.get('quantity', '?')} @ ${signal.get('price', 0):.2f} "
                f"→ {target_label} webhook ({response.status_code})"
            )
            return result

        except Exception as e:
            log.warning(f"TradersPost mirror notification FAILED for {symbol}: {e}")
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
