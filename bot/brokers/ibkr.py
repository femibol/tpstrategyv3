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
import asyncio
import threading
from datetime import datetime
from collections import defaultdict

from bot.brokers.base import BaseBroker
from bot.utils.logger import get_logger

log = get_logger("broker.ibkr")

try:
    from ib_insync import IB, Stock, Option, MarketOrder, LimitOrder, StopOrder, util, Order
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

        # Real-time streaming data
        self._streaming_contracts = {}   # symbol -> Contract
        self._streaming_tickers = {}     # symbol -> Ticker object
        self._live_prices = {}           # symbol -> {bid, ask, last, volume, ...}
        self._live_bars = {}             # symbol -> list of 5-sec bars
        self._live_ticks = {}            # symbol -> {ticker, callback} for tick-by-tick
        self._stream_lock = threading.Lock()

        # Track symbols that fail contract qualification (e.g. delisted)
        # Prevents repeated error 200 "No security definition" requests
        # Reset every 30 minutes to retry transiently-failed symbols
        self._invalid_symbols = set()
        self._invalid_symbols_reset_time = time.time()
        self._invalid_symbols_ttl = 1800  # 30 minutes

        # News callback (set by subscribe_news)
        self._news_callback = None

    def connect(self):
        """Connect to IBKR TWS/Gateway."""
        if not HAS_IB:
            log.error("ib_insync not installed. Run: pip install ib_insync")
            return False

        # Ensure an asyncio event loop exists in this thread
        # (ib_insync requires one; background threads like APScheduler don't have one)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

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

            # Quiet ib_insync's own noisy logger (Error 200, Unknown contract, etc.)
            import logging as _logging
            _logging.getLogger('ib_insync.wrapper').setLevel(_logging.CRITICAL)
            _logging.getLogger('ib_insync.ib').setLevel(_logging.CRITICAL)

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

    def is_symbol_invalid(self, symbol):
        """Check if a symbol is known to be invalid/delisted."""
        return symbol in self._invalid_symbols

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
            **kwargs:
                outside_rth: Allow trading outside regular hours (default False)
                take_profit: Take-profit price for bracket orders
                stop_loss: Stop-loss price for bracket orders
                asset_type: "option" for options orders
                expiry, strike, right: Option contract params

        Returns:
            dict with order details or None if failed
        """
        if not self.is_connected():
            log.error("Not connected to IBKR - cannot place order")
            return None

        if symbol in self._invalid_symbols:
            log.warning(f"Cannot place order for '{symbol}' - known invalid/delisted symbol")
            return None

        # SHORT-SELL GUARD: Before sending a SELL, verify we actually hold shares
        # at the broker. Prevents accidental naked shorts when internal state is stale.
        if action.upper() == "SELL" and kwargs.get("asset_type") != "option":
            try:
                broker_qty = 0
                for pos in self.ib.positions():
                    if pos.contract.symbol == symbol and pos.position > 0:
                        broker_qty = int(pos.position)
                        break
                if broker_qty <= 0:
                    log.error(
                        f"SHORT-SELL BLOCKED: SELL {quantity} {symbol} but broker "
                        f"holds {broker_qty} shares. Refusing to create short position."
                    )
                    return None
                if quantity > broker_qty:
                    log.warning(
                        f"SELL QTY CAPPED: {symbol} requested {quantity} but broker "
                        f"only holds {broker_qty}. Capping to prevent short."
                    )
                    quantity = broker_qty
            except Exception as e:
                log.warning(f"Could not verify broker position for {symbol}: {e} — proceeding cautiously")

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

            # Outside regular trading hours support
            outside_rth = kwargs.get("outside_rth", False)

            # --- Bracket Order (entry + stop-loss + take-profit as linked orders) ---
            bracket_tp = kwargs.get("take_profit")
            bracket_sl = kwargs.get("stop_loss")

            if bracket_tp and bracket_sl and order_type.upper() == "LIMIT" and limit_price:
                return self._place_bracket_order(
                    contract, symbol, action, quantity,
                    limit_price, bracket_sl, bracket_tp, outside_rth
                )

            # --- Single Order ---
            if order_type.upper() == "MARKET":
                order = MarketOrder(action.upper(), quantity)
            elif order_type.upper() == "MIDPRICE":
                # MIDPRICE: fills at the midpoint between bid/ask or better
                # Free price improvement on every trade vs chasing the ask
                order = LimitOrder(action.upper(), quantity, limit_price or 0)
                order.orderType = "MIDPRICE"
                if limit_price:
                    order.lmtPrice = limit_price  # Cap: won't pay more than this
                log.info(f"MIDPRICE order: {action} {quantity} {symbol} (cap ${limit_price or 'none'})")
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

            # Set time in force and outside-RTH flag
            order.tif = "DAY"
            if outside_rth:
                order.outsideRth = True

            # Place the order
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)  # Wait for order acknowledgement

            order_id = trade.order.orderId
            asset_label = f"{symbol} {kwargs.get('right', '')}{kwargs.get('strike', '')} {kwargs.get('expiry', '')}" \
                if kwargs.get("asset_type") == "option" else symbol

            log.info(
                f"Order placed: {action} {quantity} {asset_label} "
                f"@ {order_type} {limit_price or stop_price or 'MKT'} "
                f"{'[OUTSIDE RTH] ' if outside_rth else ''}"
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

    def _place_bracket_order(self, contract, symbol, action, quantity,
                             entry_price, stop_loss_price, take_profit_price,
                             outside_rth=False):
        """
        Place a bracket order: entry + stop-loss + take-profit as linked OCA orders.
        IBKR manages the stop and target server-side — no need for the bot to
        monitor prices for these exits (faster, survives bot restarts).

        Returns:
            dict with order details including child order IDs, or None if failed.
        """
        try:
            bracket = self.ib.bracketOrder(
                action=action.upper(),
                quantity=quantity,
                limitPrice=entry_price,
                takeProfitPrice=take_profit_price,
                stopLossPrice=stop_loss_price,
            )

            # bracket is a list of 3 orders: [parent, takeProfit, stopLoss]
            parent_order, tp_order, sl_order = bracket

            # Apply outside-RTH to all legs
            if outside_rth:
                parent_order.outsideRth = True
                tp_order.outsideRth = True
                sl_order.outsideRth = True

            parent_order.tif = "DAY"
            tp_order.tif = "GTC"  # Stop & target stay active until cancelled
            sl_order.tif = "GTC"

            # Place all three orders
            parent_trade = self.ib.placeOrder(contract, parent_order)
            self.ib.placeOrder(contract, tp_order)
            self.ib.placeOrder(contract, sl_order)
            self.ib.sleep(1)

            parent_id = parent_trade.order.orderId
            log.info(
                f"BRACKET ORDER placed: {action} {quantity} {symbol} "
                f"@ LIMIT ${entry_price:.2f} | "
                f"TP: ${take_profit_price:.2f} | SL: ${stop_loss_price:.2f} | "
                f"Parent ID: {parent_id}"
            )

            return {
                "order_id": parent_id,
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "order_type": "BRACKET",
                "asset_type": "stock",
                "limit_price": entry_price,
                "stop_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "bracket": True,
                "tp_order_id": tp_order.orderId,
                "sl_order_id": sl_order.orderId,
                "status": parent_trade.orderStatus.status,
                "time": datetime.now().isoformat(),
            }

        except Exception as e:
            log.error(f"Bracket order failed for {symbol}: {e}")
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
        if symbol in self._invalid_symbols:
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
        """Get account summary from IBKR using accountValues (no subscription needed)."""
        if not self.is_connected():
            return None

        try:
            # Use accountValues() which reads from the already-subscribed account updates
            # This avoids reqAccountSummary subscription stacking (Error 322)
            values = self.ib.accountValues()

            summary = {}
            tag_map = {
                "NetLiquidation": "net_liquidation",
                "TotalCashValue": "cash",
                "UnrealizedPnL": "unrealized_pnl",
                "RealizedPnL": "realized_pnl",
                "BuyingPower": "buying_power",
            }

            for item in values:
                if item.tag in tag_map and item.currency == "USD":
                    summary[tag_map[item.tag]] = float(item.value)

            return summary if summary else None

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

        # Periodically reset invalid symbols to retry transiently-failed contracts
        now = time.time()
        if now - self._invalid_symbols_reset_time > self._invalid_symbols_ttl:
            if self._invalid_symbols:
                log.info(f"Resetting {len(self._invalid_symbols)} blacklisted symbols for retry")
            self._invalid_symbols.clear()
            self._invalid_symbols_reset_time = now

        if symbol in self._invalid_symbols:
            return None

        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            if contract.conId == 0:
                self._invalid_symbols.add(symbol)
                log.warning(f"Unknown contract: {contract} - skipping historical bars")
                return None

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

    # =========================================================================
    # Real-Time Streaming Market Data
    # =========================================================================

    def subscribe_market_data(self, symbols):
        """
        Subscribe to real-time market data for a list of symbols.
        Uses IBKR's reqMktData for live bid/ask/last/volume streaming.

        This gives you TRUE real-time prices (no 15-min delay).
        Essential for RVOL momentum trading.
        """
        if not self.is_connected() or not HAS_IB:
            return False

        subscribed = 0
        for symbol in symbols:
            if symbol in self._streaming_contracts:
                continue  # Already subscribed
            if symbol in self._invalid_symbols:
                continue  # Known invalid/delisted symbol

            try:
                contract = Stock(symbol, "SMART", "USD")
                self.ib.qualifyContracts(contract)

                # conId == 0 means IBKR couldn't resolve the contract
                if contract.conId == 0:
                    self._invalid_symbols.add(symbol)
                    log.warning(f"Unknown contract: {contract} - skipping")
                    continue

                self._streaming_contracts[symbol] = contract

                # Request streaming market data
                # genericTickList="" gets basic bid/ask/last/volume
                # 233 = RTVolume (real-time volume for RVOL)
                ticker = self.ib.reqMktData(contract, genericTickList="233", snapshot=False)
                self._streaming_tickers[symbol] = ticker

                # Initialize live price entry
                self._live_prices[symbol] = {
                    "bid": None, "ask": None, "last": None,
                    "volume": 0, "high": None, "low": None,
                    "close": None, "open": None,
                    "last_update": None,
                }

                subscribed += 1
                log.debug(f"Subscribed to live data: {symbol}")

            except Exception as e:
                log.debug(f"Failed to subscribe {symbol}: {e}")

        if subscribed > 0:
            log.info(f"Subscribed to {subscribed} live market data streams")

            # Register tick handler if not already
            if not hasattr(self, '_tick_handler_registered'):
                self.ib.pendingTickersEvent += self._on_pending_tickers
                self._tick_handler_registered = True

        return subscribed > 0

    def unsubscribe_market_data(self, symbols=None):
        """Unsubscribe from market data streams."""
        if not self.is_connected():
            return

        syms = symbols or list(self._streaming_contracts.keys())
        for symbol in syms:
            contract = self._streaming_contracts.pop(symbol, None)
            if contract:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass
            self._streaming_tickers.pop(symbol, None)

    def subscribe_realtime_bars(self, symbols):
        """
        Subscribe to 5-second real-time bars for symbols.
        These are the fastest bar updates IBKR provides.
        Perfect for catching RVOL surges in real-time.
        """
        if not self.is_connected() or not HAS_IB:
            return False

        for symbol in symbols:
            if symbol in self._live_bars:
                continue
            if symbol in self._invalid_symbols:
                continue  # Known invalid/delisted symbol

            try:
                contract = Stock(symbol, "SMART", "USD")
                self.ib.qualifyContracts(contract)

                if contract.conId == 0:
                    self._invalid_symbols.add(symbol)
                    log.warning(f"Unknown contract: {contract} - skipping real-time bars")
                    continue

                bars = self.ib.reqRealTimeBars(
                    contract,
                    barSize=5,              # 5-second bars (only option)
                    whatToShow="TRADES",
                    useRTH=False,           # Include extended hours
                )

                self._live_bars[symbol] = {
                    "bars": bars,
                    "recent": [],  # Store last 60 bars (5 minutes of 5-sec data)
                }

                log.debug(f"Subscribed to 5-sec bars: {symbol}")

            except Exception as e:
                log.debug(f"Failed to subscribe 5-sec bars for {symbol}: {e}")

        return True

    def get_live_price(self, symbol):
        """
        Get the latest real-time price for a symbol.
        Returns immediately from streaming cache (no API call).
        """
        with self._stream_lock:
            ticker = self._streaming_tickers.get(symbol)
            if ticker:
                # Pull latest from ticker object
                price_data = {
                    "bid": ticker.bid if ticker.bid > 0 else None,
                    "ask": ticker.ask if ticker.ask > 0 else None,
                    "last": ticker.last if ticker.last > 0 else None,
                    "volume": int(ticker.volume) if ticker.volume and ticker.volume == ticker.volume else 0,
                    "high": ticker.high if ticker.high > 0 else None,
                    "low": ticker.low if ticker.low > 0 else None,
                    "close": ticker.close if ticker.close > 0 else None,
                    "open": ticker.open if ticker.open > 0 else None,
                }

                # Best price: last trade, or midpoint of bid/ask
                if price_data["last"] and price_data["last"] > 0:
                    price_data["price"] = price_data["last"]
                elif price_data["bid"] and price_data["ask"]:
                    price_data["price"] = (price_data["bid"] + price_data["ask"]) / 2
                else:
                    price_data["price"] = None

                return price_data

        return None

    def get_live_quote(self, symbol):
        """
        Get a full real-time quote from streaming data.
        Faster than get_quote() since it reads from the live stream.
        """
        price_data = self.get_live_price(symbol)
        if not price_data or not price_data.get("price"):
            return None

        price = price_data["price"]
        prev_close = price_data.get("close", 0)

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "bid": price_data.get("bid"),
            "ask": price_data.get("ask"),
            "last": price_data.get("last"),
            "volume": price_data.get("volume", 0),
            "high": price_data.get("high"),
            "low": price_data.get("low"),
            "prev_close": prev_close,
            "change": round(price - prev_close, 2) if prev_close else 0,
            "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "market_state": "REGULAR",
            "source": "IBKR_LIVE",
        }

    def get_all_live_prices(self):
        """Get all streaming prices as a dict {symbol: price}."""
        prices = {}
        for symbol in self._streaming_tickers:
            data = self.get_live_price(symbol)
            if data and data.get("price"):
                prices[symbol] = data["price"]
        return prices

    def _on_pending_tickers(self, tickers):
        """Callback fired when streaming tickers have new data."""
        with self._stream_lock:
            for ticker in tickers:
                symbol = ticker.contract.symbol if ticker.contract else None
                if symbol and symbol in self._live_prices:
                    self._live_prices[symbol]["last_update"] = datetime.now()

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
        if errorCode in (162, 2104, 2106, 2158):
            return

        # Error 200 = "No security definition" (delisted or invalid symbol)
        # Suppress entirely — calling code (qualifyContracts + conId==0 check)
        # already handles blacklisting in _invalid_symbols
        if errorCode == 200:
            symbol = None
            if contract and hasattr(contract, 'symbol'):
                symbol = contract.symbol
            if symbol and symbol not in self._invalid_symbols:
                self._invalid_symbols.add(symbol)
                log.warning(f"Blacklisting '{symbol}' - no security definition (likely delisted)")
            return

        # Error 201 = "Order rejected - 15 orders limit on same side for this contract"
        # Auto-cancel stale orders for the symbol to unblock future orders
        if errorCode == 201 and "minimum of 15 orders" in str(errorString):
            symbol = None
            if contract and hasattr(contract, 'symbol'):
                symbol = contract.symbol
            if symbol:
                log.warning(f"Order limit hit for {symbol} — auto-cancelling stale orders")
                self.cancel_symbol_orders(symbol)
            return

        # Error 300 = "Can't find EId" (stale ticker reference, non-critical)
        if errorCode == 300:
            return

        log.warning(f"IBKR Error {errorCode}: {errorString}")

    # --- IBKR News Integration ---

    def subscribe_news(self, callback=None):
        """
        Subscribe to IBKR real-time news ticks.
        News ticks fire for all subscribed contracts automatically.

        Args:
            callback: Function(news_tick_dict) called for each news headline.
        """
        if not self.is_connected():
            return False

        self._news_callback = callback
        self.ib.tickNewsEvent += self._on_news_tick

        # Also subscribe to IB bulletins (system-wide news)
        try:
            self.ib.reqNewsBulletins(allMessages=False)
        except Exception as e:
            log.debug(f"Failed to subscribe to IB bulletins: {e}")

        log.info("IBKR real-time news subscription active")
        return True

    def _on_news_tick(self, news):
        """Handle incoming IBKR news tick."""
        try:
            headline = getattr(news, 'headline', '') or ''
            provider = getattr(news, 'providerCode', '') or ''
            article_id = getattr(news, 'articleId', '') or ''
            extra_data = getattr(news, 'extraData', '') or ''

            # extraData format: "K:symbol" (e.g. "K:AAPL")
            symbol = ''
            if extra_data:
                for part in extra_data.split(':'):
                    part = part.strip()
                    if part and part.isalpha() and 1 <= len(part) <= 5:
                        symbol = part.upper()

            if not headline:
                return

            tick_dict = {
                'headline': headline,
                'provider': provider,
                'article_id': article_id,
                'symbol': symbol,
                'source': 'ibkr',
            }

            if self._news_callback:
                self._news_callback(tick_dict)

        except Exception as e:
            log.debug(f"News tick error: {e}")

    def get_news_providers(self):
        """Get available IBKR news providers (e.g. BZ=Benzinga, FLY=Flyonthewall)."""
        if not self.is_connected():
            return []
        try:
            providers = self.ib.reqNewsProviders()
            return [{'code': p.code, 'name': p.name} for p in providers]
        except Exception as e:
            log.debug(f"Failed to get news providers: {e}")
            return []

    def get_news_article(self, provider_code, article_id):
        """Fetch full article body from IBKR."""
        if not self.is_connected():
            return None
        try:
            article = self.ib.reqNewsArticle(provider_code, article_id)
            if article:
                return {
                    'type': article.articleType,
                    'text': article.articleText,
                }
        except Exception as e:
            log.debug(f"Failed to fetch article {article_id}: {e}")
        return None

    def _on_disconnect(self):
        """Handle disconnection."""
        self._connected = False
        log.warning("IBKR disconnected")

    # =========================================================================
    # Real-Time Account PnL (IBKR native — no manual calculation needed)
    # =========================================================================

    def subscribe_account_pnl(self):
        """
        Subscribe to real-time account PnL updates from IBKR.
        Fires continuously with unrealized/realized/daily PnL.
        Much more accurate than manual price * qty calculations.
        """
        if not self.is_connected():
            return False

        try:
            self._pnl_data = {"daily": 0, "unrealized": 0, "realized": 0}
            self.ib.reqPnL(account=self.ib.managedAccounts()[0])
            self.ib.pnlEvent += self._on_pnl_update
            log.info("Subscribed to real-time account PnL")
            return True
        except Exception as e:
            log.debug(f"Failed to subscribe to PnL: {e}")
            return False

    def _on_pnl_update(self, pnl):
        """Handle real-time PnL updates from IBKR."""
        try:
            self._pnl_data = {
                "daily": float(pnl.dailyPnL) if pnl.dailyPnL == pnl.dailyPnL else 0,
                "unrealized": float(pnl.unrealizedPnL) if pnl.unrealizedPnL == pnl.unrealizedPnL else 0,
                "realized": float(pnl.realizedPnL) if pnl.realizedPnL == pnl.realizedPnL else 0,
            }
        except Exception:
            pass

    def get_realtime_pnl(self):
        """Get the latest real-time PnL from streaming subscription."""
        return getattr(self, '_pnl_data', None)

    # =========================================================================
    # Enhanced 5-Second Bar Callback for Ultra-Fast Scalping
    # =========================================================================

    def subscribe_realtime_bars_with_callback(self, symbols, callback):
        """
        Subscribe to 5-second real-time bars with a callback for instant processing.
        Each bar fires callback(symbol, bar) with OHLCV data every 5 seconds.
        This is the fastest data IBKR provides — perfect for RVOL scalp.

        Args:
            symbols: List of symbols to subscribe
            callback: Function(symbol, bar_dict) called every 5 seconds per symbol
        """
        if not self.is_connected() or not HAS_IB:
            return False

        subscribed = 0
        for symbol in symbols:
            if symbol in self._live_bars:
                continue
            if symbol in self._invalid_symbols:
                continue

            try:
                contract = Stock(symbol, "SMART", "USD")
                self.ib.qualifyContracts(contract)

                if contract.conId == 0:
                    self._invalid_symbols.add(symbol)
                    continue

                bars = self.ib.reqRealTimeBars(
                    contract,
                    barSize=5,
                    whatToShow="TRADES",
                    useRTH=False,
                )

                self._live_bars[symbol] = {
                    "bars": bars,
                    "recent": [],
                    "callback": callback,
                }

                # Attach per-symbol bar update handler
                bars.updateEvent += lambda bars_list, sym=symbol: self._on_realtime_bar(sym, bars_list)

                subscribed += 1
                log.debug(f"Subscribed to 5-sec bars with callback: {symbol}")

            except Exception as e:
                log.debug(f"Failed to subscribe 5-sec bars for {symbol}: {e}")

        if subscribed > 0:
            log.info(f"5-sec real-time bars active for {subscribed} symbols")
        return subscribed > 0

    def _on_realtime_bar(self, symbol, bars_list):
        """Handle incoming 5-second real-time bar."""
        try:
            if not bars_list:
                return
            bar = bars_list[-1]
            bar_dict = {
                "time": bar.time,
                "open": float(bar.open_),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
                "count": int(bar.count),
                "wap": float(bar.wap),
            }

            # Store in recent buffer (last 720 bars = 1 hour of 5-sec data)
            entry = self._live_bars.get(symbol)
            if entry:
                entry["recent"].append(bar_dict)
                if len(entry["recent"]) > 720:
                    entry["recent"] = entry["recent"][-720:]

                # Fire callback
                cb = entry.get("callback")
                if cb:
                    cb(symbol, bar_dict)

        except Exception as e:
            log.debug(f"5-sec bar error for {symbol}: {e}")

    def get_recent_5sec_bars(self, symbol, count=60):
        """Get recent 5-second bars for a symbol (default: last 5 minutes)."""
        entry = self._live_bars.get(symbol)
        if entry and entry.get("recent"):
            return entry["recent"][-count:]
        return []

    # =========================================================================
    # Tick-by-Tick Data (fastest possible — every trade print)
    # =========================================================================

    def subscribe_tick_by_tick(self, symbols, callback):
        """
        Subscribe to tick-by-tick trade data for instant processing.
        Fires callback(symbol, tick_dict) on EVERY trade print — faster than
        5-sec bars by orders of magnitude on active stocks.

        Uses IBKR reqTickByTickData with 'AllLast' (all exchanges).
        Each subscription uses one market data line (same pool as reqMktData).

        Args:
            symbols: List of symbols to subscribe
            callback: Function(symbol, tick_dict) called on every trade
        """
        if not self.is_connected() or not HAS_IB:
            return False

        subscribed = 0
        for symbol in symbols:
            if symbol in self._live_ticks:
                continue
            if symbol in self._invalid_symbols:
                continue

            try:
                contract = Stock(symbol, "SMART", "USD")
                self.ib.qualifyContracts(contract)

                if contract.conId == 0:
                    self._invalid_symbols.add(symbol)
                    continue

                ticker = self.ib.reqTickByTickData(
                    contract,
                    tickType="AllLast",
                    numberOfTicks=0,
                    ignoreSize=False,
                )

                self._live_ticks[symbol] = {
                    "ticker": ticker,
                    "contract": contract,
                    "callback": callback,
                }

                # Fire handler on every tick update for this symbol
                ticker.updateEvent += lambda t, sym=symbol: self._on_tick_data(sym, t)

                subscribed += 1
                log.debug(f"Subscribed to tick-by-tick: {symbol}")

            except Exception as e:
                log.debug(f"Failed to subscribe tick-by-tick for {symbol}: {e}")

        if subscribed > 0:
            log.info(f"Tick-by-tick active for {subscribed} symbols (every trade print)")
        return subscribed > 0

    def _on_tick_data(self, symbol, ticker):
        """Handle incoming tick-by-tick trade data."""
        try:
            ticks = ticker.tickByTicks
            if not ticks:
                return
            tick = ticks[-1]

            price = float(tick.price) if hasattr(tick, 'price') and tick.price else 0
            size = int(tick.size) if hasattr(tick, 'size') and tick.size else 0

            if price <= 0:
                return

            tick_dict = {
                "time": tick.time if hasattr(tick, 'time') else None,
                "price": price,
                "size": size,
                "exchange": getattr(tick, 'exchange', ''),
            }

            # Update live prices cache
            with self._stream_lock:
                if symbol in self._live_prices:
                    self._live_prices[symbol]["last"] = price
                    self._live_prices[symbol]["last_update"] = time.time()

            # Fire callback
            entry = self._live_ticks.get(symbol)
            if entry:
                cb = entry.get("callback")
                if cb:
                    cb(symbol, tick_dict)

        except Exception as e:
            log.debug(f"Tick-by-tick error for {symbol}: {e}")

    def unsubscribe_tick_by_tick(self, symbols=None):
        """Cancel tick-by-tick subscriptions."""
        if not self.is_connected():
            return

        targets = symbols or list(self._live_ticks.keys())
        for symbol in targets:
            entry = self._live_ticks.pop(symbol, None)
            if entry and entry.get("ticker"):
                try:
                    self.ib.cancelTickByTickData(entry["ticker"])
                    log.debug(f"Unsubscribed tick-by-tick: {symbol}")
                except Exception as e:
                    log.debug(f"Failed to cancel tick-by-tick for {symbol}: {e}")

    # =========================================================================
    # Open Orders Management
    # =========================================================================

    def get_open_orders(self):
        """Get all open/pending orders from IBKR."""
        if not self.is_connected():
            return []

        try:
            trades = self.ib.openTrades()
            orders = []
            for trade in trades:
                orders.append({
                    "order_id": trade.order.orderId,
                    "symbol": trade.contract.symbol,
                    "action": trade.order.action,
                    "quantity": trade.order.totalQuantity,
                    "order_type": trade.order.orderType,
                    "limit_price": trade.order.lmtPrice,
                    "status": trade.orderStatus.status,
                    "filled": trade.orderStatus.filled,
                    "remaining": trade.orderStatus.remaining,
                })
            return orders
        except Exception as e:
            log.error(f"Failed to get open orders: {e}")
            return []

    def cancel_symbol_orders(self, symbol, side=None):
        """Cancel all open orders for a specific symbol (optionally filtered by side).

        Args:
            symbol: The ticker symbol to cancel orders for.
            side: Optional 'BUY' or 'SELL' to only cancel one side.

        Returns:
            Number of orders cancelled.
        """
        if not self.is_connected():
            return 0

        cancelled = 0
        try:
            for trade in self.ib.openTrades():
                if trade.contract.symbol == symbol:
                    if side and trade.order.action != side:
                        continue
                    try:
                        self.ib.cancelOrder(trade.order)
                        cancelled += 1
                    except Exception:
                        pass
            if cancelled:
                log.info(f"Cancelled {cancelled} stale orders for {symbol}" +
                         (f" ({side} side)" if side else ""))
        except Exception as e:
            log.error(f"Failed to cancel orders for {symbol}: {e}")
        return cancelled

    def cancel_all_orders(self):
        """Cancel all open orders. Use for emergency stop."""
        if not self.is_connected():
            return False
        try:
            self.ib.reqGlobalCancel()
            log.info("Global cancel sent for all open orders")
            return True
        except Exception as e:
            log.error(f"Global cancel failed: {e}")
            return False

    def close_all_positions(self):
        """Close all open positions with market orders. Use for emergency flatten."""
        if not self.is_connected():
            log.error("Not connected to IBKR - cannot close positions")
            return False

        positions = self.get_positions()
        if not positions:
            log.info("No open positions to close")
            return True

        # Cancel all pending orders first
        self.cancel_all_orders()
        self.ib.sleep(1)

        closed = 0
        for symbol, pos in positions.items():
            try:
                # Buy to close shorts, Sell to close longs
                action = "BUY" if pos["direction"] == "short" else "SELL"
                qty = pos["quantity"]
                contract = Stock(symbol, "SMART", "USD")
                self.ib.qualifyContracts(contract)

                if contract.conId == 0:
                    log.warning(f"Cannot qualify {symbol} for closing - try manually")
                    continue

                order = MarketOrder(action, qty)
                order.outsideRth = True
                trade = self.ib.placeOrder(contract, order)
                self.ib.sleep(1)
                log.info(
                    f"FLATTEN: {action} {qty} {symbol} | "
                    f"Status: {trade.orderStatus.status}"
                )
                closed += 1
            except Exception as e:
                log.error(f"Failed to close {symbol}: {e}")

        log.info(f"Flatten complete: {closed}/{len(positions)} positions closed")
        return closed == len(positions)
