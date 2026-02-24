"""
Polygon.io Full-Market Data Provider (v3 Official Client)
Primary data source for scanning, real-time prices, and historical bars.

Uses the official polygon-api-client library with v3 endpoints:
  - get_snapshot_all("stocks") → Full-market snapshot of ALL US tickers
  - list_aggs() → Historical OHLCV bars with auto-pagination
  - Built-in retry, rate-limit handling, and typed response models

Free tier: 5 calls/min — 1 snapshot + 4 bar fetches per cycle.
"""
import time
from datetime import datetime, timedelta

import pandas as pd
from polygon import RESTClient
from polygon.rest.models import TickerSnapshot, Agg
from polygon.exceptions import BadResponse

from bot.utils.logger import get_logger

log = get_logger("data.polygon")


class PolygonScanner:
    """
    Full-market data provider using the official Polygon.io Python client.
    Handles scanning, real-time prices, and historical bars.
    """

    # Rate limit: free tier = 5/min, starter = 100/min
    MIN_INTERVAL = 15  # seconds between full scans (4/min, within free tier)

    def __init__(self, api_key):
        self.api_key = api_key
        self.enabled = bool(api_key)
        self._client = None
        self._last_scan_time = 0
        self._cached_movers = []
        self._cached_runners = []
        self._cached_gap_ups = []

        # Price cache from snapshot — {symbol: {price, prev_close, volume, change_pct, ...}}
        self._price_cache = {}
        self._price_cache_time = 0

        # Bars rate limiting
        self._bars_call_times = []

        # Float data cache — {symbol: {"float": int, "shares_outstanding": int, "fetched": timestamp}}
        self._float_cache = {}
        self._float_fetch_times = []  # Rate limit tracking

        # Sector cache — {symbol: "Technology"} — from Polygon ticker details
        self._sector_cache = {}
        # Known sector mappings for common watchlist symbols (avoids API calls)
        self._known_sectors = {
            # Tech / Growth
            "SOFI": "Financials", "HOOD": "Financials", "AFRM": "Financials",
            "UPST": "Financials", "COIN": "Financials", "PYPL": "Financials",
            "SQ": "Financials",
            # Quantum / AI
            "IONQ": "Technology", "RGTI": "Technology", "AI": "Technology",
            "BBAI": "Technology", "SOUN": "Technology", "PLTR": "Technology",
            # Crypto miners
            "MARA": "Crypto", "RIOT": "Crypto", "CLSK": "Crypto",
            "CIFR": "Crypto", "MSTR": "Crypto",
            # EV / Clean energy
            "RIVN": "EV/Clean", "LCID": "EV/Clean", "NIO": "EV/Clean",
            "PLUG": "EV/Clean", "CHPT": "EV/Clean", "ENPH": "EV/Clean",
            "FSLR": "EV/Clean", "QS": "EV/Clean",
            # Biotech
            "DNA": "Healthcare", "MRNA": "Healthcare", "HIMS": "Healthcare",
            # Space / Defense
            "RKLB": "Aerospace", "LUNR": "Aerospace", "ASTS": "Aerospace",
            "JOBY": "Aerospace", "SPCE": "Aerospace",
            # Meme / High vol
            "GME": "Consumer", "AMC": "Consumer", "WISH": "Consumer",
            "OPEN": "Real Estate", "SNAP": "Technology", "RBLX": "Technology",
            "SKLZ": "Technology", "GSAT": "Technology",
        }

        if self.enabled:
            self._client = RESTClient(
                api_key=api_key,
                retries=2,
                trace=False,
            )
            log.info("Polygon.io ENABLED (v3 official client) — primary data source")
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
        if not self.enabled or not self._client:
            return [], [], []

        # Rate limit
        now = time.time()
        if now - self._last_scan_time < self.MIN_INTERVAL:
            return self._cached_movers, self._cached_runners, self._cached_gap_ups

        try:
            tickers = self._client.get_snapshot_all("stocks")
            self._last_scan_time = time.time()

            movers = []
            runners = []
            gap_ups = []
            price_cache = {}
            ticker_count = 0

            for t in tickers:
                if not isinstance(t, TickerSnapshot):
                    continue

                sym = t.ticker or ""
                if not sym or "." in sym or len(sym) > 5:
                    continue

                ticker_count += 1

                # Extract data from typed TickerSnapshot model
                day = t.day
                prev_day = t.prev_day
                change_pct = t.todays_change_perc or 0

                price = 0
                volume = 0
                prev_close = 0
                prev_volume = 1
                open_price = 0

                if isinstance(day, Agg):
                    price = day.close or 0
                    volume = day.volume or 0
                    open_price = day.open or 0

                # Fallback to last trade if day close is missing
                if price <= 0 and hasattr(t, 'last_trade') and t.last_trade:
                    price = getattr(t.last_trade, 'price', 0) or 0

                if isinstance(prev_day, Agg):
                    prev_close = prev_day.close or 0
                    prev_volume = prev_day.volume or 1

                if price <= 0:
                    continue

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
                    "float_shares": self._float_cache.get(sym, {}).get("float", 0),
                    "sector": self.get_sector(sym),
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
            self._price_cache_time = time.time()

            log.info(
                f"Polygon scan: {ticker_count} tickers | "
                f"{len(movers)} movers (2%+) | {len(runners)} runners (10%+) | "
                f"{len(gap_ups)} gap-ups (5%+)"
            )

            return movers, runners, gap_ups

        except BadResponse as e:
            if "429" in str(e):
                log.warning("Polygon rate limited — using cached results")
            else:
                log.warning(f"Polygon scan failed: {e}")
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
    # Historical Bars (Polygon Aggregates API via list_aggs)
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
        Uses list_aggs() with auto-pagination for complete data.

        Returns pandas DataFrame with columns: open, high, low, close, volume
        """
        if not self.enabled or not self._client:
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

        try:
            self._bars_call_times.append(time.time())

            aggs = []
            for a in self._client.list_aggs(
                ticker=symbol,
                multiplier=multiplier,
                timespan=timespan,
                from_=from_date,
                to=to_date,
                adjusted=True,
                sort="asc",
                limit=5000,
            ):
                aggs.append(a)

            if not aggs:
                return None

            rows = []
            for a in aggs:
                rows.append({
                    "timestamp": a.timestamp,
                    "open": a.open,
                    "high": a.high,
                    "low": a.low,
                    "close": a.close,
                    "volume": a.volume,
                })

            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["timestamp"], unit="ms")
            df = df[["open", "high", "low", "close", "volume"]]
            df = df.dropna(subset=["close"])
            return df if not df.empty else None

        except BadResponse as e:
            if "429" in str(e):
                log.debug(f"Polygon bars rate limited for {symbol}")
            else:
                log.debug(f"Polygon bars {symbol}: {e}")
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

    def get_float(self, symbol):
        """Get float estimate for a symbol. Uses cached data if available.
        Float data is fetched from Polygon ticker details API.
        Rate limited: max 1 float fetch per cycle to conserve API calls."""
        if symbol in self._float_cache:
            return self._float_cache[symbol].get("float", 0)
        return 0

    def _fetch_float_batch(self, symbols, max_fetches=2):
        """Fetch float data for a batch of symbols. Max 2 fetches per call
        to conserve Polygon rate limits (free tier: 5 calls/min)."""
        if not self.enabled or not self._client:
            return

        fetched = 0
        for sym in symbols:
            if sym in self._float_cache:
                continue
            if fetched >= max_fetches:
                break
            if not self._can_make_bar_call():
                break

            try:
                self._bars_call_times.append(time.time())
                details = self._client.get_ticker_details(sym)
                if details:
                    shares_out = getattr(details, 'share_class_shares_outstanding', 0) or 0
                    weighted = getattr(details, 'weighted_shares_outstanding', 0) or 0
                    # Float estimate: use weighted outstanding, fallback to share class
                    float_est = weighted or shares_out
                    # Also grab sector/industry from ticker details (free data)
                    sic_desc = getattr(details, 'sic_description', '') or ''
                    sector = self._classify_sector(sic_desc)
                    self._float_cache[sym] = {
                        "float": int(float_est),
                        "shares_outstanding": int(shares_out),
                        "sector": sector,
                        "fetched": time.time(),
                    }
                    if sector != "Unknown":
                        self._sector_cache[sym] = sector
                    fetched += 1
                    if float_est > 0:
                        log.debug(f"Float data for {sym}: {float_est/1e6:.1f}M shares")
            except Exception as e:
                log.debug(f"Float fetch failed for {sym}: {e}")
                self._float_cache[sym] = {"float": 0, "shares_outstanding": 0, "sector": "Unknown", "fetched": time.time()}
                fetched += 1

    def _classify_sector(self, sic_description):
        """Classify SIC description into a broad sector category."""
        if not sic_description:
            return "Unknown"
        desc = sic_description.lower()
        if any(k in desc for k in ["software", "computer", "electronic", "semiconductor", "telecom"]):
            return "Technology"
        if any(k in desc for k in ["bank", "financ", "insurance", "credit", "securi"]):
            return "Financials"
        if any(k in desc for k in ["pharma", "biolog", "medic", "health", "hospital"]):
            return "Healthcare"
        if any(k in desc for k in ["oil", "gas", "petrol", "energy", "mining", "coal"]):
            return "Energy"
        if any(k in desc for k in ["retail", "food", "restaurant", "apparel", "consumer"]):
            return "Consumer"
        if any(k in desc for k in ["motor", "vehicle", "auto", "aircraft", "aerospace"]):
            return "Industrials"
        if any(k in desc for k in ["real estate", "reit", "property"]):
            return "Real Estate"
        return "Other"

    def get_sector(self, symbol):
        """Get sector for a symbol. Checks local cache first, then known mappings.
        For unknown symbols discovered by scanner, fetches from Polygon ticker details
        (piggybacks on float fetch to avoid extra API calls)."""
        sym = symbol.upper()
        # Check caches
        if sym in self._sector_cache:
            return self._sector_cache[sym]
        if sym in self._known_sectors:
            return self._known_sectors[sym]
        # Check if we got sector from a previous float fetch
        float_entry = self._float_cache.get(sym, {})
        if "sector" in float_entry:
            self._sector_cache[sym] = float_entry["sector"]
            return float_entry["sector"]
        return "Unknown"

    def get_sector_counts(self, symbols):
        """Get count of symbols per sector. Used by risk manager for correlation limits."""
        counts = {}
        for sym in symbols:
            sector = self.get_sector(sym)
            counts[sector] = counts.get(sector, 0) + 1
        return counts

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
