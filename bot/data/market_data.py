"""
Market Data Feed - provides REAL price data to strategies.

Data sources (in priority order):
1. IBKR real-time (PRIMARY — streaming + historical via TWS/IB Gateway)
2. Polygon.io (fallback — real-time prices from snapshot, bars from aggregates)
3. Yahoo Finance direct API (last resort, ~15 min delay)

No fake data. No simulated prices.
"""
import time
import json
from datetime import datetime, timedelta

import requests as _requests
import numpy as np
import pandas as pd

from bot.utils.logger import get_logger

log = get_logger("data.market_data")

# yfinance is optional last-resort fallback
try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


class MarketDataFeed:
    """
    Real market data provider with multiple sources and caching.

    Priority: IBKR -> Polygon.io -> Yahoo Finance -> yfinance lib
    """

    def __init__(self, config, broker=None, polygon=None):
        self.config = config
        self.broker = broker
        self.polygon = polygon  # PolygonScanner instance
        self.bar_size = config.settings.get("data", {}).get("bar_size", "5 mins")
        self.lookback_days = config.settings.get("data", {}).get("lookback_days", 30)

        # Log data source status
        if self.broker and self.broker.is_connected():
            log.info("IBKR connected — primary data source (real-time streaming + bars)")
        elif self.polygon and self.polygon.enabled:
            log.info("IBKR not connected — using Polygon.io as data source (real-time)")
        else:
            log.warning(
                "IBKR not connected, Polygon API key not set — falling back to Yahoo (15-min delay). "
                "Connect to IBKR or set POLYGON_API_KEY for real-time data."
            )

        # Cache
        self._bars_cache = {}       # symbol -> DataFrame (standard bars)
        self._bars_1m_cache = {}    # symbol -> DataFrame (1-min bars for scalping)
        self._price_cache = {}      # symbol -> price
        self._volume_cache = {}     # symbol -> volume
        self._last_update = {}      # symbol -> timestamp
        self._last_1m_update = {}   # symbol -> timestamp
        self._cache_ttl = config.settings.get("data", {}).get("cache_ttl", 10)
        self._cache_ttl_1m = 5      # 5-second TTL for 1-min bar cache
        self._bars_fail_cache = {}  # symbol -> timestamp of last failed bar fetch
        self._bars_fail_ttl = 120   # Retry failed bar fetches every 2 minutes (not every cycle)

        # Streaming state (IBKR only)
        self._streaming_active = False
        self._subscribed_symbols = set()
        # IBKR paper accounts allow max 100 simultaneous streams
        self._max_ibkr_streams = config.settings.get("data", {}).get("max_ibkr_streams", 95)

    def prune_stale_streams(self, active_symbols):
        """Unsubscribe IBKR streams for symbols no longer actively tracked.
        active_symbols should include positions + current universe/watchlist."""
        if not self.broker or not hasattr(self.broker, 'unsubscribe_market_data'):
            return 0
        active_set = set(s.upper() for s in active_symbols)
        stale = self._subscribed_symbols - active_set
        if not stale:
            return 0
        try:
            self.broker.unsubscribe_market_data(list(stale))
            self._subscribed_symbols -= stale
            log.info(
                f"Pruned {len(stale)} stale IBKR streams — "
                f"now {len(self._subscribed_symbols)}/{self._max_ibkr_streams}"
            )
        except Exception as e:
            log.debug(f"Stream prune error: {e}")
        return len(stale)

    def start_streaming(self, symbols):
        """Start IBKR real-time streaming for symbols (capped at _max_ibkr_streams)."""
        if not self.broker or not self.broker.is_connected():
            return False
        if not hasattr(self.broker, 'subscribe_market_data'):
            return False

        new_symbols = [s for s in symbols if s not in self._subscribed_symbols]

        # Filter out symbols IBKR has blacklisted (delisted/invalid)
        if hasattr(self.broker, 'is_symbol_invalid'):
            new_symbols = [s for s in new_symbols if not self.broker.is_symbol_invalid(s)]

        if not new_symbols:
            return self._streaming_active

        # Cap subscriptions to stay under IBKR limit
        remaining_capacity = self._max_ibkr_streams - len(self._subscribed_symbols)
        if remaining_capacity <= 0:
            log.warning(
                f"IBKR stream limit reached ({len(self._subscribed_symbols)}/{self._max_ibkr_streams}). "
                f"Skipping {len(new_symbols)} new symbols."
            )
            return self._streaming_active
        if len(new_symbols) > remaining_capacity:
            log.warning(
                f"Trimming IBKR subscriptions: requested {len(new_symbols)}, "
                f"capacity {remaining_capacity}/{self._max_ibkr_streams}"
            )
            new_symbols = new_symbols[:remaining_capacity]

        try:
            result = self.broker.subscribe_market_data(new_symbols)
            if result:
                # Only track symbols that weren't blacklisted during subscription
                if hasattr(self.broker, 'is_symbol_invalid'):
                    valid = [s for s in new_symbols if not self.broker.is_symbol_invalid(s)]
                else:
                    valid = new_symbols
                self._subscribed_symbols.update(valid)
                self._streaming_active = True
                log.info(f"IBKR streaming active for {len(self._subscribed_symbols)} symbols")
                return True
        except Exception as e:
            log.debug(f"Failed to start streaming: {e}")

        return False

    def update(self, symbols):
        """Fetch latest real data for all symbols."""
        now = time.time()

        # Start streaming for new symbols if IBKR is connected
        if self.broker and self.broker.is_connected():
            new_syms = [s for s in symbols if s not in self._subscribed_symbols]
            if new_syms:
                self.start_streaming(new_syms)

        # Bulk-update prices from Polygon snapshot cache (free, no API calls)
        if self.polygon and self.polygon.enabled and self.polygon.price_cache_age < 60:
            poly_prices = self.polygon.get_snapshots_batch(symbols)
            for sym, price in poly_prices.items():
                if price > 0:
                    self._price_cache[sym] = price
                    self._last_update[sym] = now

        for symbol in symbols:
            # If streaming is active, grab live prices from IBKR stream first
            if self._streaming_active and self.broker and hasattr(self.broker, 'get_live_price'):
                live = self.broker.get_live_price(symbol)
                if live and live.get("price"):
                    self._price_cache[symbol] = live["price"]
                    if live.get("volume"):
                        self._volume_cache[symbol] = live["volume"]
                    self._last_update[symbol] = now

            last = self._last_update.get(symbol, 0)
            if now - last < self._cache_ttl:
                continue

            # Skip symbols that recently failed bar fetch (avoid hammering APIs)
            fail_time = self._bars_fail_cache.get(symbol, 0)
            if fail_time and now - fail_time < self._bars_fail_ttl:
                continue

            try:
                bars = self._fetch_bars(symbol)
                if bars is not None and len(bars) > 0:
                    self._bars_cache[symbol] = bars
                    if symbol not in self._subscribed_symbols:
                        self._price_cache[symbol] = float(bars["close"].iloc[-1])
                    self._volume_cache[symbol] = float(bars["volume"].iloc[-1])
                    self._last_update[symbol] = now
                    self._bars_fail_cache.pop(symbol, None)  # Clear failure on success
                    log.debug(f"Updated {symbol}: ${self._price_cache.get(symbol, 0):.2f}")
                else:
                    self._bars_fail_cache[symbol] = now
            except Exception as e:
                self._bars_fail_cache[symbol] = now
                log.debug(f"Data update failed for {symbol}: {e}")

    def _fetch_bars(self, symbol):
        """Fetch real bars from available sources (no fake data)."""
        bars = None

        # 1. IBKR (real-time, highest quality)
        if self.broker and self.broker.is_connected():
            try:
                bars = self.broker.get_historical_bars(
                    symbol,
                    duration=f"{self.lookback_days} D",
                    bar_size=self.bar_size
                )
                if bars is not None and len(bars) > 0:
                    return bars
            except Exception as e:
                log.debug(f"IBKR data failed for {symbol}: {e}")

        # 2. Polygon.io aggregates (real-time with API key)
        if bars is None and self.polygon and self.polygon.enabled:
            try:
                bars = self.polygon.fetch_bars(symbol, self.bar_size, self.lookback_days)
                if bars is not None and len(bars) > 0:
                    return bars
            except Exception as e:
                log.debug(f"Polygon data failed for {symbol}: {e}")

        # 3. Yahoo Finance direct API (no dependency issues)
        if bars is None:
            try:
                bars = self._fetch_yahoo_direct(symbol)
                if bars is not None and len(bars) > 0:
                    return bars
            except Exception as e:
                log.debug(f"Yahoo direct failed for {symbol}: {e}")

        # 4. yfinance library (if installed and working)
        if bars is None and HAS_YF:
            try:
                bars = self._fetch_yfinance(symbol)
                if bars is not None and len(bars) > 0:
                    return bars
            except Exception as e:
                log.debug(f"yfinance lib failed for {symbol}: {e}")

        return bars

    # =========================================================================
    # Yahoo Finance Direct API (fallback - ~15 minute delay)
    # =========================================================================

    def _fetch_yahoo_direct(self, symbol):
        """Fetch real market data directly from Yahoo Finance API."""
        interval_map = {
            "1 min": "1m",
            "5 mins": "5m",
            "15 mins": "15m",
            "30 mins": "30m",
            "1 hour": "1h",
            "1 day": "1d",
        }
        interval = interval_map.get(self.bar_size, "5m")

        if interval == "1m":
            range_str = "5d"
        elif interval in ("5m", "15m", "30m"):
            range_str = f"{min(self.lookback_days, 59)}d"
        elif interval == "1h":
            range_str = f"{min(self.lookback_days, 729)}d"
        else:
            range_str = f"{self.lookback_days}d"

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval={interval}&range={range_str}"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        resp = _requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            log.debug(f"Yahoo API returned {resp.status_code} for {symbol}")
            return None

        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        chart = result[0]
        timestamps = chart.get("timestamp", [])
        quote = chart.get("indicators", {}).get("quote", [{}])[0]

        if not timestamps or not quote:
            return None

        df = pd.DataFrame({
            "open": quote.get("open", []),
            "high": quote.get("high", []),
            "low": quote.get("low", []),
            "close": quote.get("close", []),
            "volume": quote.get("volume", []),
        }, index=pd.to_datetime(timestamps, unit="s"))

        df = df.dropna(subset=["close"])
        return df if not df.empty else None

    def _fetch_yfinance(self, symbol):
        """Fetch data from yfinance library (last-resort fallback)."""
        if not HAS_YF:
            return None

        ticker = yf.Ticker(symbol)
        interval_map = {
            "1 min": "1m", "5 mins": "5m", "15 mins": "15m",
            "30 mins": "30m", "1 hour": "1h", "1 day": "1d",
        }
        interval = interval_map.get(self.bar_size, "5m")

        if interval in ("1m",):
            period = "5d"
        elif interval in ("5m", "15m", "30m"):
            period = f"{min(self.lookback_days, 59)}d"
        else:
            period = f"{self.lookback_days}d"

        df = ticker.history(period=period, interval=interval)
        if df.empty:
            return None

        df.columns = [c.lower() for c in df.columns]
        if "adj close" in df.columns:
            df = df.drop(columns=["adj close"])
        return df

    # =========================================================================
    # Public Data Access Methods
    # =========================================================================

    def get_bars(self, symbol, periods=None, bar_size=None):
        """Get cached historical bars for a symbol."""
        # If requesting 1-min bars, try 1-min cache first
        if bar_size and "1" in bar_size and "min" in bar_size.lower():
            bars = self._bars_1m_cache.get(symbol)
            if bars is not None:
                if periods and len(bars) > periods:
                    return bars.iloc[-periods:]
                return bars

        bars = self._bars_cache.get(symbol)
        if bars is None:
            return None

        if periods and len(bars) > periods:
            return bars.iloc[-periods:]
        return bars

    def update_1m_bars(self, symbols):
        """Fetch 1-minute bars for scalp strategy symbols."""
        now = time.time()
        for symbol in symbols:
            last = self._last_1m_update.get(symbol, 0)
            if now - last < self._cache_ttl_1m:
                continue
            try:
                bars = self._fetch_bars_1m(symbol)
                if bars is not None and len(bars) > 0:
                    self._bars_1m_cache[symbol] = bars
                    self._price_cache[symbol] = float(bars["close"].iloc[-1])
                    self._volume_cache[symbol] = float(bars["volume"].iloc[-1])
                    self._last_1m_update[symbol] = now
            except Exception as e:
                log.debug(f"1-min data failed for {symbol}: {e}")

    def _fetch_bars_1m(self, symbol):
        """Fetch 1-minute bars from available sources."""
        # 1. IBKR
        if self.broker and self.broker.is_connected():
            try:
                bars = self.broker.get_historical_bars(
                    symbol, duration="2 D", bar_size="1 min"
                )
                if bars is not None and len(bars) > 0:
                    return bars
            except Exception:
                pass

        # 2. Polygon.io aggregates (1-min bars)
        if self.polygon and self.polygon.enabled:
            try:
                bars = self.polygon.fetch_bars(symbol, bar_size="1 min", lookback_days=2)
                if bars is not None and len(bars) > 0:
                    return bars
            except Exception:
                pass

        # 3. Yahoo Finance direct (1m bars, 2-day range)
        try:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                f"?interval=1m&range=2d"
            )
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = _requests.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("chart", {}).get("result", [])
                if result:
                    chart = result[0]
                    timestamps = chart.get("timestamp", [])
                    quote = chart.get("indicators", {}).get("quote", [{}])[0]
                    if timestamps and quote:
                        df = pd.DataFrame({
                            "open": quote.get("open", []),
                            "high": quote.get("high", []),
                            "low": quote.get("low", []),
                            "close": quote.get("close", []),
                            "volume": quote.get("volume", []),
                        }, index=pd.to_datetime(timestamps, unit="s"))
                        df = df.dropna(subset=["close"])
                        if not df.empty:
                            return df
        except Exception:
            pass

        # 4. yfinance library fallback
        if HAS_YF:
            try:
                ticker = yf.Ticker(symbol)
                df = ticker.history(period="2d", interval="1m")
                if not df.empty:
                    df.columns = [c.lower() for c in df.columns]
                    if "adj close" in df.columns:
                        df = df.drop(columns=["adj close"])
                    return df
            except Exception:
                pass

        return None

    def refresh_prices(self, symbols):
        """Rapid REAL-TIME price refresh for position monitoring.
        Priority: IBKR streaming -> Polygon snapshot (real-time) -> Yahoo (delayed).
        Polygon prices come from the cached full-market snapshot — no extra API calls."""
        # 1. IBKR streaming (instant, batch)
        if self._streaming_active and self.broker and hasattr(self.broker, 'get_live_price'):
            for symbol in symbols:
                try:
                    live = self.broker.get_live_price(symbol)
                    if live and live.get("price"):
                        self._price_cache[symbol] = live["price"]
                except Exception:
                    pass
            return

        # 2. Polygon snapshot prices (real-time, from cached scan — no API calls)
        if self.polygon and self.polygon.enabled and self.polygon.price_cache_age < 60:
            prices = self.polygon.get_snapshots_batch(symbols)
            if prices:
                self._price_cache.update(prices)
                return  # Polygon succeeded, skip Yahoo

        # 3. Yahoo fallback (15-min delayed — only if no Polygon)
        for symbol in symbols:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = _requests.get(url, headers=headers, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    result = data.get("chart", {}).get("result", [])
                    if result:
                        meta = result[0].get("meta", {})
                        price = meta.get("regularMarketPrice", 0)
                        if price and price > 0:
                            self._price_cache[symbol] = price
            except Exception:
                pass

    def _is_crypto(self, symbol):
        """Check if symbol is a crypto ticker."""
        return any(symbol.upper().endswith(s) for s in ("-USD", "-USDT", "-BTC", "-ETH"))

    def get_price(self, symbol):
        """Get latest real price for a symbol."""
        return self._price_cache.get(symbol)

    def get_volume(self, symbol):
        """Get latest volume for a symbol."""
        return self._volume_cache.get(symbol)

    def get_all_prices(self):
        """Get all cached prices."""
        return dict(self._price_cache)

    def get_data(self, symbol):
        """Get cached bar data for a symbol (alias for get_bars)."""
        return self._bars_cache.get(symbol)

    def get_quote(self, symbol):
        """
        Get a real-time quote for a single symbol.
        Priority: IBKR streaming -> Polygon snapshot (real-time) -> Yahoo (delayed).
        """
        # 1. Try IBKR live streaming (TRUE real-time, no delay)
        if self._streaming_active and self.broker and hasattr(self.broker, 'get_live_quote'):
            try:
                quote = self.broker.get_live_quote(symbol)
                if quote and quote.get("price"):
                    return quote
            except Exception:
                pass

        # 2. Polygon snapshot (real-time from cached full-market scan)
        if self.polygon and self.polygon.enabled:
            try:
                snap = self.polygon.get_snapshot(symbol)
                if snap and snap.get("price"):
                    self._price_cache[symbol] = snap["price"]
                    if snap.get("volume"):
                        self._volume_cache[symbol] = snap["volume"]
                    return {
                        **snap,
                        "market_state": "OPEN",
                    }
            except Exception as e:
                log.debug(f"Polygon quote failed for {symbol}: {e}")

        # 3. Yahoo fallback (~15 min delay)
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = _requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("chart", {}).get("result", [])
                if result:
                    meta = result[0].get("meta", {})
                    price = meta.get("regularMarketPrice", 0)
                    prev_close = meta.get("chartPreviousClose", 0)
                    if price and price > 0:
                        return {
                            "symbol": symbol,
                            "price": price,
                            "prev_close": prev_close,
                            "change": price - prev_close if prev_close else 0,
                            "change_pct": ((price - prev_close) / prev_close * 100) if prev_close else 0,
                            "market_state": meta.get("marketState", "UNKNOWN"),
                            "source": "YAHOO",
                        }
        except Exception as e:
            log.debug(f"Quote failed for {symbol}: {e}")

        return None
