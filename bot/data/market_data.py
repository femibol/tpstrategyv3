"""
Market Data Feed - provides price data to strategies.
Uses IBKR as primary, yfinance as fallback.
"""
import time
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd

from bot.utils.logger import get_logger

log = get_logger("data.market_data")

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


class MarketDataFeed:
    """
    Market data provider with caching and fallback.

    Primary: IBKR real-time data
    Fallback: yfinance (delayed but free)
    """

    def __init__(self, config, broker=None):
        self.config = config
        self.broker = broker
        self.primary = config.settings.get("data", {}).get("primary", "ibkr")
        self.fallback = config.settings.get("data", {}).get("fallback", "yfinance")
        self.bar_size = config.settings.get("data", {}).get("bar_size", "5 mins")
        self.lookback_days = config.settings.get("data", {}).get("lookback_days", 30)

        # Cache
        self._bars_cache = {}  # symbol -> DataFrame
        self._price_cache = {}  # symbol -> price
        self._volume_cache = {}  # symbol -> volume
        self._last_update = {}  # symbol -> timestamp
        self._cache_ttl = 30  # seconds

    def update(self, symbols):
        """Fetch latest data for all symbols."""
        now = time.time()

        for symbol in symbols:
            last = self._last_update.get(symbol, 0)
            if now - last < self._cache_ttl:
                continue  # Skip if recently updated

            try:
                bars = self._fetch_bars(symbol)
                if bars is not None and len(bars) > 0:
                    self._bars_cache[symbol] = bars
                    self._price_cache[symbol] = float(bars["close"].iloc[-1])
                    self._volume_cache[symbol] = float(bars["volume"].iloc[-1])
                    self._last_update[symbol] = now
            except Exception as e:
                log.debug(f"Data update failed for {symbol}: {e}")

    def _fetch_bars(self, symbol):
        """Fetch bars from primary source, fallback if needed."""
        bars = None

        # Try IBKR first
        if self.primary == "ibkr" and self.broker and self.broker.is_connected():
            try:
                bars = self.broker.get_historical_bars(
                    symbol,
                    duration=f"{self.lookback_days} D",
                    bar_size=self.bar_size
                )
            except Exception as e:
                log.debug(f"IBKR data failed for {symbol}: {e}")

        # Fallback to yfinance
        if bars is None and HAS_YF:
            try:
                bars = self._fetch_yfinance(symbol)
            except Exception as e:
                log.debug(f"yfinance failed for {symbol}: {e}")

        return bars

    def _fetch_yfinance(self, symbol):
        """Fetch data from yfinance."""
        ticker = yf.Ticker(symbol)

        # Map bar size to yfinance interval
        interval_map = {
            "1 min": "1m",
            "5 mins": "5m",
            "15 mins": "15m",
            "30 mins": "30m",
            "1 hour": "1h",
            "1 day": "1d",
        }
        interval = interval_map.get(self.bar_size, "5m")

        # yfinance limits: 1m=7d, 5m=60d, 15m=60d, 1h=730d, 1d=unlimited
        if interval in ("1m",):
            period = "5d"
        elif interval in ("5m", "15m", "30m"):
            period = f"{min(self.lookback_days, 59)}d"
        else:
            period = f"{self.lookback_days}d"

        df = ticker.history(period=period, interval=interval)

        if df.empty:
            return None

        # Normalize column names
        df.columns = [c.lower() for c in df.columns]
        if "adj close" in df.columns:
            df = df.drop(columns=["adj close"])

        return df

    def get_bars(self, symbol, periods=None):
        """
        Get historical bars for a symbol.

        Args:
            symbol: Stock ticker
            periods: Number of recent bars to return (None = all cached)

        Returns:
            DataFrame with columns: open, high, low, close, volume
        """
        bars = self._bars_cache.get(symbol)
        if bars is None:
            return None

        if periods and len(bars) > periods:
            return bars.iloc[-periods:]

        return bars

    def get_price(self, symbol):
        """Get latest price for a symbol."""
        return self._price_cache.get(symbol)

    def get_volume(self, symbol):
        """Get latest volume for a symbol."""
        return self._volume_cache.get(symbol)

    def get_all_prices(self):
        """Get all cached prices."""
        return dict(self._price_cache)
