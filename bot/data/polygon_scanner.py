"""
Polygon.io Full-Market Data Provider
Primary data source for scanning, real-time prices, and historical bars.

Snapshot API: /v2/snapshot/locale/us/markets/stocks/tickers
  → Scans ALL ~10,000 US stocks in ONE call (prices, volume, change %)
  → Used for scanning AND real-time price cache

Aggregates API: /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
  → Historical OHLCV bars for individual symbols

Free tier: 5 calls/min — 1 snapshot + 4 bar fetches per cycle.
"""
import time
from datetime import datetime, timedelta

import requests
import pandas as pd

from bot.utils.logger import get_logger

log = get_logger("data.polygon")


class PolygonScanner:
    """
    Full-market data provider using Polygon.io.
    Handles scanning, real-time prices, and historical bars.
    """

    SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
    AGGS_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"

    # Rate limit: free tier = 5/min, starter = 100/min
    MIN_INTERVAL = 15  # seconds between full scans (4/min, within free tier)

    def __init__(self, api_key):
        self.api_key = api_key
        self.enabled = bool(api_key)
        self._last_scan_time = 0
        self._cached_movers = []
        self._cached_runners = []
        self._cached_gap_ups = []

        # Price cache from snapshot — {symbol: {price, prev_close, volume, change_pct, ...}}
        self._price_cache = {}
        self._price_cache_time = 0

        # Bars rate limiting
        self._bars_call_times = []

        if self.enabled:
            log.info("Polygon.io ENABLED — primary data source for scanning + prices + bars")
        else:
            log.info("Polygon.io disabled — set POLYGON_API_KEY to enable")

    # =========================================================================
    # Full-Market Snapshot (scanning + price cache)
    # =========================================================================

    def scan_full_market(self, min_change_pct=2.0, min_price=0.50, max_price=50.0, min_volume=50000):
        """
        Scan the entire US market for movers.
        Also caches prices for ALL tickers (used by market_data for quotes).
        Filters movers to $0.50-$50 range for maximum % gains.

        Returns tuple: (movers, runners, gap_ups)
        """
        if not self.enabled:
            return [], [], []

        # Rate limit
        now = time.time()
        if now - self._last_scan_time < self.MIN_INTERVAL:
            return self._cached_movers, self._cached_runners, self._cached_gap_ups

        try:
            resp = requests.get(
                self.SNAPSHOT_URL,
                params={"apiKey": self.api_key},
                timeout=15,
            )

            if resp.status_code == 429:
                log.warning("Polygon rate limited — using cached results")
                return self._cached_movers, self._cached_runners, self._cached_gap_ups

            if resp.status_code != 200:
                log.warning(f"Polygon scan failed: HTTP {resp.status_code}")
                return self._cached_movers, self._cached_runners, self._cached_gap_ups

            data = resp.json()
            tickers = data.get("tickers", [])
            self._last_scan_time = now

            movers = []
            runners = []
            gap_ups = []
            price_cache = {}

            for t in tickers:
                sym = t.get("ticker", "")
                if not sym or "." in sym or len(sym) > 5:
                    continue

                day = t.get("day", {})
                prev_day = t.get("prevDay", {})
                todaysChangePerc = t.get("todaysChangePerc", 0)

                price = day.get("c", 0) or t.get("lastTrade", {}).get("p", 0)
                volume = day.get("v", 0) or 0
                prev_close = prev_day.get("c", 0) or 0
                prev_volume = prev_day.get("v", 1) or 1
                open_price = day.get("o", 0) or 0

                if price <= 0:
                    continue

                change_pct = todaysChangePerc or 0

                # Cache price for ALL valid tickers (used for real-time quotes)
                price_cache[sym] = {
                    "price": round(price, 2),
                    "prev_close": round(prev_close, 2),
                    "volume": int(volume),
                    "change_pct": round(change_pct, 2),
                    "open": round(open_price, 2),
                }

                # Apply mover filters ($0.50-$50 range for max % gains)
                if price < min_price:
                    continue
                if max_price and price > max_price:
                    continue
                if volume < min_volume:
                    continue
                if abs(change_pct) < min_change_pct:
                    continue

                rvol = round(volume / prev_volume, 1) if prev_volume > 0 else 0
                gap_pct = ((open_price - prev_close) / prev_close * 100) if prev_close > 0 and open_price > 0 else 0

                entry = {
                    "symbol": sym,
                    "name": sym,
                    "price": round(price, 2),
                    "change_pct": round(change_pct, 2),
                    "volume": int(volume),
                    "avg_volume": int(prev_volume),
                    "rvol": rvol,
                    "gap_pct": round(gap_pct, 2),
                    "prev_close": round(prev_close, 2),
                    "open": round(open_price, 2),
                    "market_cap": 0,
                    "source": "polygon",
                }

                if change_pct >= 2.0:
                    movers.append(entry)
                if change_pct >= 10.0 and price <= max_price:
                    runners.append(entry)
                if gap_pct >= 5.0:
                    gap_ups.append(entry)

            movers.sort(key=lambda x: x["change_pct"], reverse=True)
            runners.sort(key=lambda x: x["change_pct"], reverse=True)
            gap_ups.sort(key=lambda x: x["gap_pct"], reverse=True)

            self._cached_movers = movers
            self._cached_runners = runners
            self._cached_gap_ups = gap_ups
            self._price_cache = price_cache
            self._price_cache_time = now

            log.info(
                f"Polygon scan: {len(tickers)} tickers | "
                f"{len(movers)} movers (2%+) | {len(runners)} runners (10%+) | "
                f"{len(gap_ups)} gap-ups (5%+)"
            )

            return movers, runners, gap_ups

        except requests.exceptions.Timeout:
            log.warning("Polygon scan timeout")
            return self._cached_movers, self._cached_runners, self._cached_gap_ups
        except Exception as e:
            log.warning(f"Polygon scan error: {e}")
            return self._cached_movers, self._cached_runners, self._cached_gap_ups

    # =========================================================================
    # Real-Time Prices (from cached snapshot data — no extra API calls)
    # =========================================================================

    def get_price(self, symbol):
        """Get cached real-time price for a symbol (from last snapshot)."""
        entry = self._price_cache.get(symbol)
        return entry["price"] if entry else None

    def get_all_prices(self):
        """Get {symbol: price} dict for ALL cached tickers."""
        return {sym: d["price"] for sym, d in self._price_cache.items()}

    def get_snapshot(self, symbol):
        """Get full snapshot data for a symbol from cache."""
        entry = self._price_cache.get(symbol)
        if not entry:
            return None
        return {
            "symbol": symbol,
            "price": entry["price"],
            "prev_close": entry["prev_close"],
            "change": round(entry["price"] - entry["prev_close"], 2) if entry["prev_close"] else 0,
            "change_pct": entry["change_pct"],
            "volume": entry["volume"],
            "source": "POLYGON",
        }

    def get_snapshots_batch(self, symbols):
        """Get {symbol: price} for a batch of symbols from cache."""
        result = {}
        for sym in symbols:
            entry = self._price_cache.get(sym)
            if entry and entry["price"] > 0:
                result[sym] = entry["price"]
        return result

    @property
    def price_cache_age(self):
        """Seconds since last snapshot update."""
        if self._price_cache_time == 0:
            return float("inf")
        return time.time() - self._price_cache_time

    # =========================================================================
    # Historical Bars (Polygon Aggregates API)
    # =========================================================================

    def _can_make_bar_call(self):
        """Check if we can make another API call within rate limits."""
        now = time.time()
        # Keep only calls within the last 60 seconds
        self._bars_call_times = [t for t in self._bars_call_times if now - t < 60]
        # Reserve 1 call/min for snapshot, allow 4 for bars
        return len(self._bars_call_times) < 4

    def fetch_bars(self, symbol, bar_size="5 mins", lookback_days=30):
        """
        Fetch historical OHLCV bars from Polygon aggregates API.
        Rate-limited to 4 calls/min (reserves 1/min for snapshot scan).

        Returns pandas DataFrame with columns: open, high, low, close, volume
        """
        if not self.enabled:
            return None

        if not self._can_make_bar_call():
            log.debug(f"Polygon bars rate limited, skipping {symbol}")
            return None

        interval_map = {
            "1 min": (1, "minute"),
            "5 mins": (5, "minute"),
            "15 mins": (15, "minute"),
            "30 mins": (30, "minute"),
            "1 hour": (1, "hour"),
            "1 day": (1, "day"),
        }
        multiplier, timespan = interval_map.get(bar_size, (5, "minute"))

        to_date = datetime.utcnow().strftime("%Y-%m-%d")
        from_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        url = self.AGGS_URL.format(
            ticker=symbol,
            multiplier=multiplier,
            timespan=timespan,
            from_date=from_date,
            to_date=to_date,
        )

        try:
            self._bars_call_times.append(time.time())
            resp = requests.get(
                url,
                params={"apiKey": self.api_key, "adjusted": "true", "sort": "asc", "limit": 5000},
                timeout=10,
            )

            if resp.status_code == 429:
                log.debug(f"Polygon bars rate limited for {symbol}")
                return None

            if resp.status_code != 200:
                log.debug(f"Polygon bars {symbol}: HTTP {resp.status_code}")
                return None

            data = resp.json()
            results = data.get("results", [])
            if not results:
                return None

            df = pd.DataFrame(results)
            df = df.rename(columns={
                "t": "timestamp", "o": "open", "h": "high",
                "l": "low", "c": "close", "v": "volume",
            })
            df.index = pd.to_datetime(df["timestamp"], unit="ms")
            df = df[["open", "high", "low", "close", "volume"]]
            df = df.dropna(subset=["close"])
            return df if not df.empty else None

        except requests.exceptions.Timeout:
            log.debug(f"Polygon bars timeout for {symbol}")
            return None
        except Exception as e:
            log.debug(f"Polygon bars error for {symbol}: {e}")
            return None

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    def get_top_movers(self, limit=100):
        """Get top movers compatible with engine's expected format."""
        movers, _, _ = self.scan_full_market()
        return movers[:limit]

    def get_runners(self, limit=50):
        """Get explosive runners (10%+)."""
        _, runners, _ = self.scan_full_market()
        return runners[:limit]

    def get_gap_ups(self, limit=50):
        """Get pre-market gap-up stocks (5%+ gap from prev close)."""
        _, _, gap_ups = self.scan_full_market()
        return gap_ups[:limit]

    def get_losers(self, limit=100):
        """Get top losers from cached scan data ($0.50-$50 range)."""
        losers = []
        for sym, data in self._price_cache.items():
            if data["change_pct"] <= -2.0 and data["price"] >= 0.50 and data["price"] <= 50.0 and data["volume"] >= 50000:
                losers.append({
                    "symbol": sym,
                    "name": sym,
                    "price": data["price"],
                    "change_pct": data["change_pct"],
                    "volume": data["volume"],
                    "source": "polygon",
                })
        losers.sort(key=lambda x: x["change_pct"])
        return losers[:limit]
