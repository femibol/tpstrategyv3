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
import threading
from datetime import datetime
from collections import defaultdict

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

        # Real-time streaming data
        self._streaming_contracts = {}   # symbol -> Contract
        self._streaming_tickers = {}     # symbol -> Ticker object
        self._live_prices = {}           # symbol -> {bid, ask, last, volume, ...}
        self._live_bars = {}             # symbol -> list of 5-sec bars
        self._stream_lock = threading.Lock()

        # Track symbols that fail contract qualification (e.g. delisted)
        # Prevents repeated error 200 "No security definition" requests
        self._invalid_symbols = set()

        # News callback (set by subscribe_news)
        self._news_callback = None

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
            **kwargs: Option params (expiry, strike, right) for options orders

        Returns:
            dict with order details or None if failed
        """
        if not self.is_connected():
            log.error("Not connected to IBKR - cannot place order")
            return None

        if symbol in self._invalid_symbols:
            log.warning(f"Cannot place order for '{symbol}' - known invalid/delisted symbol")
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
