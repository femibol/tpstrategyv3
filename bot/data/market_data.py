"""
Market Data Feed - provides REAL price data to strategies.

Data sources (in priority order):
1. IBKR real-time (if connected)
2. Alpaca Markets (free real-time with API key)
3. Yahoo Finance direct API (free, ~15 min delay, no deps)

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

# Try importing optional providers
try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

try:
    from alpaca_trade_api.rest import REST as AlpacaREST, TimeFrame
    HAS_ALPACA = True
except ImportError:
    HAS_ALPACA = False


class MarketDataFeed:
    """
    Real market data provider with multiple sources and caching.

    Priority: IBKR -> Alpaca -> Yahoo Finance Direct -> yfinance lib
    """

    def __init__(self, config, broker=None):
        self.config = config
        self.broker = broker
        self.bar_size = config.settings.get("data", {}).get("bar_size", "5 mins")
        self.lookback_days = config.settings.get("data", {}).get("lookback_days", 30)

        # Alpaca client (if configured)
        self.alpaca = None
        alpaca_key = getattr(config, 'alpaca_api_key', '') or ''
        alpaca_secret = getattr(config, 'alpaca_secret_key', '') or ''
        if alpaca_key and alpaca_secret and HAS_ALPACA:
            try:
                base_url = getattr(config, 'alpaca_base_url', 'https://paper-api.alpaca.markets')
                self.alpaca = AlpacaREST(alpaca_key, alpaca_secret, base_url)
                log.info("Alpaca Markets data connected")
            except Exception as e:
                log.warning(f"Alpaca init failed: {e}")

        # Cache
        self._bars_cache = {}   # symbol -> DataFrame
        self._price_cache = {}  # symbol -> price
        self._volume_cache = {} # symbol -> volume
        self._last_update = {}  # symbol -> timestamp
        self._cache_ttl = 30    # seconds

    def update(self, symbols):
        """Fetch latest real data for all symbols."""
        now = time.time()

        for symbol in symbols:
            last = self._last_update.get(symbol, 0)
            if now - last < self._cache_ttl:
                continue

            try:
                bars = self._fetch_bars(symbol)
                if bars is not None and len(bars) > 0:
                    self._bars_cache[symbol] = bars
                    self._price_cache[symbol] = float(bars["close"].iloc[-1])
                    self._volume_cache[symbol] = float(bars["volume"].iloc[-1])
                    self._last_update[symbol] = now
                    log.debug(f"Updated {symbol}: ${self._price_cache[symbol]:.2f}")
            except Exception as e:
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

        # 2. Alpaca (real-time with free account)
        if bars is None and self.alpaca:
            try:
                bars = self._fetch_alpaca(symbol)
                if bars is not None and len(bars) > 0:
                    return bars
            except Exception as e:
                log.debug(f"Alpaca data failed for {symbol}: {e}")

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

    def _fetch_alpaca(self, symbol):
        """Fetch real-time data from Alpaca Markets."""
        if not self.alpaca:
            return None

        interval_map = {
            "1 min": TimeFrame.Minute,
            "5 mins": TimeFrame(5, "Min"),
            "15 mins": TimeFrame(15, "Min"),
            "30 mins": TimeFrame(30, "Min"),
            "1 hour": TimeFrame.Hour,
            "1 day": TimeFrame.Day,
        }
        timeframe = interval_map.get(self.bar_size, TimeFrame(5, "Min"))

        start = (datetime.now() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")

        bars = self.alpaca.get_bars(
            symbol,
            timeframe,
            start=start,
            end=end,
            limit=500,
        ).df

        if bars.empty:
            return None

        bars.columns = [c.lower() for c in bars.columns]
        # Ensure required columns
        if "trade_count" in bars.columns:
            bars = bars.drop(columns=["trade_count"], errors="ignore")
        if "vwap" in bars.columns:
            bars = bars.drop(columns=["vwap"], errors="ignore")

        return bars

    def _fetch_yahoo_direct(self, symbol):
        """
        Fetch real market data directly from Yahoo Finance API.
        No external library needed - just HTTP requests.
        """
        interval_map = {
            "1 min": "1m",
            "5 mins": "5m",
            "15 mins": "15m",
            "30 mins": "30m",
            "1 hour": "1h",
            "1 day": "1d",
        }
        interval = interval_map.get(self.bar_size, "5m")

        # Yahoo limits: 1m=7d, 5m/15m/30m=60d, 1h=730d, 1d=unlimited
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

        # Drop rows with NaN prices
        df = df.dropna(subset=["close"])

        if df.empty:
            return None

        return df

    def _fetch_yfinance(self, symbol):
        """Fetch data from yfinance library (fallback)."""
        if not HAS_YF:
            return None

        ticker = yf.Ticker(symbol)

        interval_map = {
            "1 min": "1m",
            "5 mins": "5m",
            "15 mins": "15m",
            "30 mins": "30m",
            "1 hour": "1h",
            "1 day": "1d",
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

    def get_bars(self, symbol, periods=None):
        """Get cached historical bars for a symbol."""
        bars = self._bars_cache.get(symbol)
        if bars is None:
            return None

        if periods and len(bars) > periods:
            return bars.iloc[-periods:]

        return bars

    def get_price(self, symbol):
        """Get latest real price for a symbol."""
        return self._price_cache.get(symbol)

    def get_volume(self, symbol):
        """Get latest volume for a symbol."""
        return self._volume_cache.get(symbol)

    def get_all_prices(self):
        """Get all cached prices."""
        return dict(self._price_cache)

    def get_quote(self, symbol):
        """Get a real-time quote for a single symbol (bypasses cache)."""
        # Try Yahoo direct for fastest single quote
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
                        }
        except Exception as e:
            log.debug(f"Quote failed for {symbol}: {e}")

        return None
