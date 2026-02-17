"""
Interactive Brokers integration via ib_insync.
Supports both paper and live trading.

Setup:
1. Install TWS or IB Gateway
2. Enable API connections in TWS: Edit > Global Config > API > Settings
3. Check "Enable ActiveX and Socket Clients"
4. Set port: 7497 (paper) or 7496 (live)
5. Set .env IBKR_PORT accordingly
"""
import time
from datetime import datetime

from bot.brokers.base import BaseBroker
from bot.utils.logger import get_logger

log = get_logger("broker.ibkr")

try:
    from ib_insync import IB, Stock, Option, MarketOrder, LimitOrder, StopOrder, util
    HAS_IB = True
except ImportError:
    HAS_IB = False
    log.warning("ib_insync not installed - IBKR broker unavailable")


class IBKRBroker(BaseBroker):
    """
    Interactive Brokers broker implementation.

    Connects to TWS or IB Gateway for order execution.
    Paper trading on port 7497, live on 7496.
    """

    def __init__(self, config):
        self.config = config
        self.host = config.ibkr_host
        self.port = config.ibkr_port
        self.client_id = config.ibkr_client_id
        self.ib = None
        self._connected = False
        self._order_id_counter = 0

    def connect(self):
        """Connect to IBKR TWS/Gateway."""
        if not HAS_IB:
            log.error("ib_insync not installed. Run: pip install ib_insync")
            return False

        try:
            self.ib = IB()
            self.ib.connect(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=20,
                readonly=False
            )
            self._connected = True

            # Register callbacks
            self.ib.orderStatusEvent += self._on_order_status
            self.ib.errorEvent += self._on_error
            self.ib.disconnectedEvent += self._on_disconnect

            mode = "PAPER" if self.port in (7497, 4002) else "LIVE"
            log.info(f"Connected to IBKR ({mode}) at {self.host}:{self.port}")
            return True

        except Exception as e:
            log.error(f"IBKR connection failed: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """Disconnect from IBKR."""
        if self.ib and self._connected:
            try:
                self.ib.disconnect()
            except Exception:
                pass
        self._connected = False
        log.info("Disconnected from IBKR")

    def is_connected(self):
        """Check connection status."""
        if self.ib:
            try:
                return self.ib.isConnected()
            except Exception:
                return False
        return False

    def reconnect(self):
        """Reconnect with retry."""
        log.info("Attempting IBKR reconnect...")
        self.disconnect()
        time.sleep(2)

        for attempt in range(3):
            if self.connect():
                return True
            wait = 2 ** (attempt + 1)
            log.warning(f"Reconnect attempt {attempt + 1} failed, waiting {wait}s")
            time.sleep(wait)

        log.error("IBKR reconnection failed after 3 attempts")
        return False

    def place_order(self, symbol, action, quantity, order_type="LIMIT",
                    limit_price=None, stop_price=None, **kwargs):
        """
        Place an order through IBKR (stocks or options).

        Args:
            symbol: Stock ticker (e.g., "AAPL")
            action: "BUY" or "SELL"
            quantity: Number of shares/contracts
            order_type: "MARKET", "LIMIT", or "STOP"
            limit_price: Price for limit orders
            stop_price: Price for stop orders
            **kwargs: Option params (expiry, strike, right) for options orders

        Returns:
            dict with order details or None if failed
        """
        if not self.is_connected():
            log.error("Not connected to IBKR - cannot place order")
            return None

        try:
            # Create contract (stock or option)
            if kwargs.get("asset_type") == "option":
                contract = self._create_option_contract(
                    symbol,
                    expiry=kwargs.get("expiry"),
                    strike=kwargs.get("strike"),
                    right=kwargs.get("right", "C"),
                )
            else:
                contract = Stock(symbol, "SMART", "USD")

            self.ib.qualifyContracts(contract)

            # Create order
            if order_type.upper() == "MARKET":
                order = MarketOrder(action.upper(), quantity)
            elif order_type.upper() == "LIMIT":
                if limit_price is None:
                    log.error(f"Limit price required for limit order: {symbol}")
                    return None
                order = LimitOrder(action.upper(), quantity, limit_price)
            elif order_type.upper() == "STOP":
                if stop_price is None:
                    log.error(f"Stop price required for stop order: {symbol}")
                    return None
                order = StopOrder(action.upper(), quantity, stop_price)
            else:
                log.error(f"Unknown order type: {order_type}")
                return None

            # Set time in force
            order.tif = "DAY"

            # Place the order
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)  # Wait for order acknowledgement

            order_id = trade.order.orderId
            asset_label = f"{symbol} {kwargs.get('right', '')}{kwargs.get('strike', '')} {kwargs.get('expiry', '')}" \
                if kwargs.get("asset_type") == "option" else symbol

            log.info(
                f"Order placed: {action} {quantity} {asset_label} "
                f"@ {order_type} {limit_price or stop_price or 'MKT'} "
                f"| Order ID: {order_id}"
            )

            return {
                "order_id": order_id,
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "order_type": order_type,
                "asset_type": kwargs.get("asset_type", "stock"),
                "limit_price": limit_price,
                "stop_price": stop_price,
                "status": trade.orderStatus.status,
                "time": datetime.now().isoformat(),
            }

        except Exception as e:
            log.error(f"Order placement failed for {symbol}: {e}")
            return None

    def _create_option_contract(self, symbol, expiry, strike, right="C"):
        """
        Create an IBKR option contract.

        Args:
            symbol: Underlying ticker (e.g., "NVDA")
            expiry: Expiration date "YYYYMMDD" (e.g., "20250117")
            strike: Strike price (e.g., 500.0)
            right: "C" for call, "P" for put
        """
        if not HAS_IB:
            return None
        return Option(symbol, expiry, strike, right, "SMART", "100", "USD")

    def get_option_chain(self, symbol):
        """
        Get the option chain for a symbol.

        Returns list of available expirations and strikes.
        """
        if not self.is_connected():
            return None

        try:
            stock = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(stock)

            chains = self.ib.reqSecDefOptParams(
                stock.symbol, "", stock.secType, stock.conId
            )

            if not chains:
                return None

            # Use SMART exchange chain
            result = []
            for chain in chains:
                if chain.exchange == "SMART":
                    result.append({
                        "exchange": chain.exchange,
                        "expirations": sorted(chain.expirations),
                        "strikes": sorted(chain.strikes),
                    })

            return result[0] if result else None

        except Exception as e:
            log.error(f"Failed to get option chain for {symbol}: {e}")
            return None

    def cancel_order(self, order_id):
        """Cancel an open order."""
        if not self.is_connected():
            return False

        try:
            for trade in self.ib.openTrades():
                if trade.order.orderId == order_id:
                    self.ib.cancelOrder(trade.order)
                    log.info(f"Order {order_id} cancelled")
                    return True
            log.warning(f"Order {order_id} not found")
            return False
        except Exception as e:
            log.error(f"Cancel order failed: {e}")
            return False

    def get_positions(self):
        """Get all open positions from IBKR."""
        if not self.is_connected():
            return {}

        try:
            positions = {}
            for pos in self.ib.positions():
                symbol = pos.contract.symbol
                if pos.position != 0:
                    positions[symbol] = {
                        "symbol": symbol,
                        "quantity": abs(pos.position),
                        "direction": "long" if pos.position > 0 else "short",
                        "avg_cost": pos.avgCost,
                        "entry_price": pos.avgCost,
                        "market_value": pos.position * pos.avgCost,
                    }
            return positions
        except Exception as e:
            log.error(f"Failed to get positions: {e}")
            return {}

    def get_account_summary(self):
        """Get account summary from IBKR."""
        if not self.is_connected():
            return None

        try:
            self.ib.reqAccountSummary()
            self.ib.sleep(1)

            summary = {}
            for item in self.ib.accountSummary():
                if item.tag == "NetLiquidation":
                    summary["net_liquidation"] = float(item.value)
                elif item.tag == "TotalCashValue":
                    summary["cash"] = float(item.value)
                elif item.tag == "UnrealizedPnL":
                    summary["unrealized_pnl"] = float(item.value)
                elif item.tag == "RealizedPnL":
                    summary["realized_pnl"] = float(item.value)
                elif item.tag == "BuyingPower":
                    summary["buying_power"] = float(item.value)

            self.ib.cancelAccountSummary()
            return summary

        except Exception as e:
            log.error(f"Failed to get account summary: {e}")
            return None

    def get_order_status(self, order_id):
        """Get status of a specific order."""
        if not self.is_connected():
            return None

        try:
            for trade in self.ib.trades():
                if trade.order.orderId == order_id:
                    return {
                        "order_id": order_id,
                        "status": trade.orderStatus.status,
                        "filled": trade.orderStatus.filled,
                        "remaining": trade.orderStatus.remaining,
                        "avg_fill_price": trade.orderStatus.avgFillPrice,
                    }
            return None
        except Exception as e:
            log.error(f"Failed to get order status: {e}")
            return None

    def get_historical_bars(self, symbol, duration="1 D", bar_size="5 mins"):
        """Get historical bars from IBKR."""
        if not self.is_connected():
            return None

        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )

            if bars:
                return util.df(bars)
            return None

        except Exception as e:
            log.error(f"Failed to get historical bars for {symbol}: {e}")
            return None

    # --- Event Callbacks ---
    def _on_order_status(self, trade):
        """Handle order status updates."""
        log.info(
            f"Order update: {trade.contract.symbol} | "
            f"Status: {trade.orderStatus.status} | "
            f"Filled: {trade.orderStatus.filled}/{trade.order.totalQuantity}"
        )

    def _on_error(self, reqId, errorCode, errorString, contract):
        """Handle errors from IBKR."""
        # Filter out common non-critical messages
        if errorCode in (2104, 2106, 2158):  # Data farm connections
            return
        log.warning(f"IBKR Error {errorCode}: {errorString}")

    def _on_disconnect(self):
        """Handle disconnection."""
        self._connected = False
        log.warning("IBKR disconnected")
