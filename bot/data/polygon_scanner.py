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

from bot.utils.logger import get_logger

log = get_logger("data.polygon")

try:
    from polygon import RESTClient
    from polygon.rest.models import TickerSnapshot, Agg
    from polygon.exceptions import BadResponse
    HAS_POLYGON = True
except (ImportError, KeyError, Exception) as e:
    HAS_POLYGON = False
    RESTClient = None
    TickerSnapshot = None
    Agg = None
    BadResponse = Exception
    log.warning(f"polygon-api-client unavailable ({type(e).__name__}): Polygon data source disabled")


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
            "XYZ": "Financials",    # was SQ (Block Inc renamed 2025)
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
            "LMT": "Aerospace", "NOC": "Aerospace", "RTX": "Aerospace",
            "GD": "Aerospace", "BA": "Aerospace", "LHX": "Aerospace",
            "AVAV": "Aerospace", "KTOS": "Aerospace",
            # Energy / Oil & Gas
            "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
            "OXY": "Energy", "APA": "Energy", "DVN": "Energy",
            "MRO": "Energy", "FANG": "Energy", "EOG": "Energy",
            "SLB": "Energy", "HAL": "Energy", "BKR": "Energy",
            "VLO": "Energy", "PSX": "Energy", "MPC": "Energy",
            "BATL": "Energy", "INDO": "Energy",
            # Shipping / Tankers
            "FRO": "Energy", "DHT": "Energy", "INSW": "Energy",
            "ZIM": "Industrials", "STNG": "Energy", "TNK": "Energy",
            "EURN": "Energy", "ASC": "Energy",
            # Airlines / Travel
            "UAL": "Travel", "AAL": "Travel", "DAL": "Travel",
            "LUV": "Travel", "JBLU": "Travel", "ALK": "Travel",
            "MAR": "Travel", "HLT": "Travel", "H": "Travel",
            "BKNG": "Travel", "EXPE": "Travel", "ABNB": "Travel",
            "RCL": "Travel", "CCL": "Travel", "NCLH": "Travel",
            # Meme / High vol
            "GME": "Consumer", "AMC": "Consumer",
            "OPEN": "Real Estate", "SNAP": "Technology", "RBLX": "Technology",
            "SKLZ": "Technology", "GSAT": "Technology",
        }

        if self.enabled and HAS_POLYGON:
            self._client = RESTClient(
                api_key=api_key,
                retries=2,
                trace=False,
            )
            log.info("Polygon.io ENABLED (v3 official client) — primary data source")
        elif self.enabled and not HAS_POLYGON:
            self.enabled = False
            log.warning("Polygon.io API key set but polygon library unavailable — disabled")
        else:
            log.info("Polygon.io disabled — set POLYGON_API_KEY to enable")

    # =========================================================================
    # Full-Market Snapshot (scanning + price cache)
    # =========================================================================

    def scan_full_market(self, min_change_pct=2.0, min_price=1.00, max_price=500.0, min_volume=50000):
        """
        Scan the entire US market for momentum movers.
        Also caches prices for ALL tickers (used by market_data for quotes).
        Filters: $1-$500 range, 2%+ movers, 5%+ runners, 3%+ gaps.

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
                    "volume": int(volume) if volume == volume else 0,
                    "avg_volume": int(prev_volume) if prev_volume == prev_volume else 0,
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
                    "volume": int(volume) if volume == volume else 0,
                    "avg_volume": int(prev_volume) if prev_volume == prev_volume else 0,
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
                    movers.append(entry)   # 2%+ movers (was 5% — too restrictive on quiet days)
                if change_pct >= 5.0:
                    runners.append(entry)  # 5%+ runners (was 10% — catch more explosive setups)
                if gap_pct >= 3.0:
                    gap_ups.append(entry)  # 3%+ gaps for pre-market session scanning

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
                f"{len(movers)} movers (5%+) | {len(runners)} runners (10%+) | "
                f"{len(gap_ups)} gap-ups (3%+)"
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
    # Top Gainers Scanner — No Price Cap, All Sessions
    # =========================================================================

    def scan_top_gainers(self, session="regular", limit=50, config=None):
        """
        Scan for the biggest percentage gainers across ALL price ranges.
        Unlike scan_full_market(), this has NO $100 price cap — catches
        mid/large-cap runners like SMCI, MSTR, RCKT, etc.

        Session-aware volume thresholds (defaults, overridden by config):
          - premarket:  10K volume (thin premarket liquidity)
          - regular:    100K volume (standard liquidity)
          - postmarket: 50K volume (reduced AH liquidity)

        Args:
            session: "premarket", "regular", or "postmarket"
            limit: max number of gainers to return
            config: optional dict from settings.yaml top_gainers section

        Returns: list of gainer dicts sorted by change_pct descending,
                 each with a "gainer_rank" field (1 = top gainer).
        """
        if not self.enabled or not self._client:
            return []

        # Use the price cache from the last scan_full_market() call — no extra API calls.
        # scan_full_market() caches ALL tickers including those it filters out by price.
        if not self._price_cache:
            log.debug("Top gainers: no price cache yet, skipping")
            return []

        # Session-aware thresholds (defaults)
        session_defaults = {
            "premarket":  {"min_volume": 10_000,  "min_change": 3.0, "min_price": 1.00},
            "regular":    {"min_volume": 100_000, "min_change": 5.0, "min_price": 2.00},
            "postmarket": {"min_volume": 50_000,  "min_change": 3.0, "min_price": 2.00},
        }
        defaults = session_defaults.get(session, session_defaults["regular"])

        # Override with config values if provided (from settings.yaml top_gainers section)
        if config and session in config:
            scfg = config[session]
            min_vol = scfg.get("min_volume", defaults["min_volume"])
            min_change = scfg.get("min_change_pct", defaults["min_change"])
            min_price = scfg.get("min_price", defaults["min_price"])
        else:
            min_vol = defaults["min_volume"]
            min_change = defaults["min_change"]
            min_price = defaults["min_price"]

        gainers = []
        for sym, data in self._price_cache.items():
            # Basic validity
            if not sym or "." in sym or len(sym) > 5:
                continue

            price = data.get("price", 0)
            change_pct = data.get("change_pct", 0)
            volume = data.get("volume", 0)

            if price < min_price:
                continue
            if volume < min_vol:
                continue
            if change_pct < min_change:
                continue

            # Calculate gap % and RVOL from cached data
            prev_close = data.get("prev_close", 0)
            avg_volume = data.get("avg_volume", 1)
            open_price = data.get("open", 0)

            rvol = round(volume / avg_volume, 1) if avg_volume > 0 else 0
            gap_pct = round((open_price - prev_close) / prev_close * 100, 2) if prev_close > 0 and open_price > 0 else 0

            gainers.append({
                "symbol": sym,
                "name": sym,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "volume": int(volume),
                "avg_volume": int(avg_volume),
                "rvol": rvol,
                "gap_pct": gap_pct,
                "prev_close": round(prev_close, 2),
                "open": round(open_price, 2),
                "market_cap": 0,
                "float_shares": self._float_cache.get(sym, {}).get("float", 0),
                "sector": self.get_sector(sym),
                "source": "polygon_top_gainers",
            })

        # Sort by change_pct descending — biggest movers first
        gainers.sort(key=lambda x: x["change_pct"], reverse=True)
        gainers = gainers[:limit]

        # Tag with rank
        for i, g in enumerate(gainers):
            g["gainer_rank"] = i + 1

        if gainers:
            top3 = ", ".join(f"{g['symbol']} +{g['change_pct']:.1f}% ${g['price']:.2f}" for g in gainers[:3])
            log.info(
                f"Top gainers ({session}): {len(gainers)} stocks above +{min_change}% | "
                f"Top 3: {top3}"
            )

        return gainers

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
                        "float": int(float_est) if float_est == float_est else 0,
                        "shares_outstanding": int(shares_out) if shares_out == shares_out else 0,
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
        if any(k in desc for k in ["oil", "gas", "petrol", "energy", "mining", "coal", "crude"]):
            return "Energy"
        if any(k in desc for k in ["airline", "air transport", "hotel", "lodging", "travel",
                                     "cruise", "booking", "rental"]):
            return "Travel"
        if any(k in desc for k in ["aircraft", "aerospace", "guided missile", "defense",
                                     "ordnance", "tank"]):
            return "Aerospace"
        if any(k in desc for k in ["retail", "food", "restaurant", "apparel", "consumer"]):
            return "Consumer"
        if any(k in desc for k in ["motor", "vehicle", "auto", "ship", "marine", "freight"]):
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

    def has_earnings_soon(self, symbol, days_ahead=1):
        """Check if a symbol has earnings within the next N days.
        Uses news catalysts as a proxy (Polygon free tier doesn't include earnings calendar).
        Returns True if recent news mentions earnings/results for this ticker."""
        # Check news-based earnings detection cache
        cache_key = f"earnings_{symbol}"
        cached = self._float_cache.get(cache_key)
        if cached and time.time() - cached.get("fetched", 0) < 3600:
            return cached.get("has_earnings", False)

        # Check Polygon news for earnings-related headlines in the last 24h
        if not self._client:
            return False

        try:
            from datetime import date
            today = date.today()
            count = 0
            for n in self._client.list_ticker_news(
                ticker=symbol,
                order="desc",
                limit=5,
                sort="published_utc",
            ):
                title = (getattr(n, 'title', '') or '').lower()
                desc = (getattr(n, 'description', '') or '').lower()
                content = f"{title} {desc}"
                earnings_keywords = [
                    "earnings", "quarterly results", "q1 ", "q2 ", "q3 ", "q4 ",
                    "quarterly report", "eps", "revenue report", "earnings call",
                    "reports after", "reports before", "reports tomorrow",
                    "fiscal quarter", "quarterly earnings",
                ]
                if any(kw in content for kw in earnings_keywords):
                    self._float_cache[cache_key] = {"has_earnings": True, "fetched": time.time()}
                    log.info(f"EARNINGS DETECTED: {symbol} has earnings-related news")
                    return True
                count += 1
                if count >= 5:
                    break
        except Exception as e:
            log.debug(f"Earnings check failed for {symbol}: {e}")

        self._float_cache[cache_key] = {"has_earnings": False, "fetched": time.time()}
        return False

    def check_unusual_options(self, symbol):
        """Check for unusual options activity (UOA) on a symbol.

        Detects when options volume vastly exceeds open interest,
        suggesting smart money is making new directional bets.

        Returns:
            dict with {bullish, bearish, call_vol, put_vol, uoa_score} or None
        """
        if not self.enabled or not self._client:
            return None

        if not self._can_make_bar_call():
            return None

        try:
            self._bars_call_times.append(time.time())

            # Get options chain snapshot for the ticker
            from polygon.rest.models import SnapshotOption
            options = self._client.list_snapshot_options_chain(symbol)

            total_call_vol = 0
            total_put_vol = 0
            total_call_oi = 0
            total_put_oi = 0
            large_sweeps = 0

            for opt in options:
                if not hasattr(opt, 'details') or not hasattr(opt, 'day'):
                    continue

                contract_type = getattr(opt.details, 'contract_type', '') or ''
                vol = getattr(opt.day, 'volume', 0) or 0
                oi = getattr(opt.day, 'open_interest', 0) or 0

                if contract_type.lower() == 'call':
                    total_call_vol += vol
                    total_call_oi += oi
                elif contract_type.lower() == 'put':
                    total_put_vol += vol
                    total_put_oi += oi

                # Flag large unusual contracts (vol > 5x OI)
                if oi > 0 and vol > 5 * oi:
                    large_sweeps += 1

            # Calculate UOA score
            uoa_score = 0
            bullish = False
            bearish = False

            # Call/put ratio
            total_vol = total_call_vol + total_put_vol
            if total_vol > 0:
                call_ratio = total_call_vol / total_vol
                if call_ratio > 0.7:
                    bullish = True
                    uoa_score += 15
                elif call_ratio < 0.3:
                    bearish = True

            # Volume vs OI (new positions being opened)
            if total_call_oi > 0 and total_call_vol > 3 * total_call_oi:
                uoa_score += 20  # Massive call buying
                bullish = True
            elif total_call_oi > 0 and total_call_vol > 2 * total_call_oi:
                uoa_score += 10

            # Large sweeps
            if large_sweeps >= 5:
                uoa_score += 15
            elif large_sweeps >= 2:
                uoa_score += 8

            if uoa_score > 0:
                log.info(
                    f"UOA: {symbol} — Call vol: {total_call_vol:,} Put vol: {total_put_vol:,} "
                    f"| Sweeps: {large_sweeps} | Score: {uoa_score} "
                    f"| {'BULLISH' if bullish else 'BEARISH' if bearish else 'NEUTRAL'}"
                )

            return {
                "bullish": bullish,
                "bearish": bearish,
                "call_vol": total_call_vol,
                "put_vol": total_put_vol,
                "call_oi": total_call_oi,
                "put_oi": total_put_oi,
                "large_sweeps": large_sweeps,
                "uoa_score": uoa_score,
            }

        except Exception as e:
            log.debug(f"UOA check failed for {symbol}: {e}")
            return None

    def estimate_short_interest(self, symbols, max_fetches=3):
        """Estimate short interest using ticker details + volume patterns.

        Polygon free tier doesn't have FINRA short interest data directly,
        but we can use:
        1. Ticker details: shares_outstanding vs weighted_shares_outstanding
        2. High short volume ratio from recent trading as a proxy
        3. Low float + high volume = squeeze candidate

        Returns:
            dict of {symbol: {short_pct (estimated), days_to_cover, avg_daily_volume}}
        """
        results = {}
        if not self.enabled or not self._client:
            return results

        fetched = 0
        for sym in symbols:
            if fetched >= max_fetches:
                break

            # Use cached float/shares data
            cached = self._float_cache.get(sym, {})
            float_shares = cached.get("float", 0)
            shares_outstanding = cached.get("shares_outstanding", 0)

            # Get current volume from price cache
            price_data = self._price_cache.get(sym, {})
            current_volume = price_data.get("volume", 0)
            avg_volume = price_data.get("avg_volume", 0) or current_volume

            if not float_shares or not avg_volume:
                continue

            # Estimate short interest from float utilization
            # Low float + high volume relative to float = likely high short interest
            # This is a rough proxy — real short interest requires FINRA data
            volume_to_float_ratio = current_volume / float_shares if float_shares > 0 else 0

            # Heuristic: if daily volume is > 30% of float, shorts are likely active
            estimated_short_pct = 0
            if volume_to_float_ratio > 0.50:
                estimated_short_pct = 35  # Very likely heavily shorted
            elif volume_to_float_ratio > 0.30:
                estimated_short_pct = 25
            elif volume_to_float_ratio > 0.15:
                estimated_short_pct = 18
            elif volume_to_float_ratio > 0.08:
                estimated_short_pct = 12

            # Boost for tiny float (< 10M shares) — these are squeeze magnets
            if float_shares < 5_000_000:
                estimated_short_pct = min(50, estimated_short_pct + 10)
            elif float_shares < 15_000_000:
                estimated_short_pct = min(50, estimated_short_pct + 5)

            if estimated_short_pct >= 12:
                # Days to cover = estimated short shares / avg daily volume
                estimated_short_shares = float_shares * (estimated_short_pct / 100)
                days_to_cover = estimated_short_shares / avg_volume if avg_volume > 0 else 0

                results[sym] = {
                    "short_pct": estimated_short_pct,
                    "shares_short": int(estimated_short_shares),
                    "days_to_cover": round(days_to_cover, 1),
                    "avg_daily_volume": int(avg_volume),
                    "float_shares": int(float_shares),
                }
                log.debug(
                    f"Short interest est: {sym} — ~{estimated_short_pct}% SI, "
                    f"DTC {days_to_cover:.1f}, float {float_shares/1e6:.1f}M"
                )

            fetched += 1

        return results

    # =========================================================================
    # Session-Aware Scanning (momentum runner spec)
    # =========================================================================

    def get_session_candidates(self, session="regular"):
        """Get session-appropriate candidates for the momentum runner strategy.

        Pre-market: Stocks gapping >3% on 2x pre-market RVOL, float <50M preferred
        Regular: Top 5 gappers that hold gap + new intraday highs on 3x volume
        Post-market: Stocks moving >5% on >500K volume after hours

        Args:
            session: "premarket", "regular", or "postmarket"

        Returns:
            list of candidate dicts sorted by priority
        """
        movers = self._cached_movers
        runners = self._cached_runners
        gap_ups = self._cached_gap_ups

        candidates = []

        if session == "premarket":
            # Pre-market: gap-ups with real movement, prefer low float
            # NOTE: RVOL during premarket is meaningless (premarket vol / full-day vol ≈ 0)
            # so we use change% and gap% as primary filters instead of RVOL
            for entry in movers + gap_ups:
                gap = entry.get("gap_pct", 0)
                change = abs(entry.get("change_pct", 0))
                volume = entry.get("volume", 0)
                float_shares = entry.get("float_shares", 0)

                # Accept: 3%+ gap OR 5%+ change (premarket RVOL is unreliable)
                if gap >= 3.0 or change >= 5.0:
                    priority = max(gap, change) * (1 + volume / 100000)
                    if 0 < float_shares < 50_000_000:
                        priority *= 2.0  # Double priority for low float
                    entry["priority"] = round(priority, 1)
                    entry["session_reason"] = f"Pre-market gap +{gap:.1f}% chg +{change:.1f}%"
                    candidates.append(entry)

        elif session == "regular":
            # Regular hours: top gappers that held + new intraday highs + halt candidates
            seen = set()

            # Top 5 gappers from pre-market that held their gap
            for entry in sorted(gap_ups, key=lambda x: x.get("gap_pct", 0), reverse=True)[:5]:
                sym = entry["symbol"]
                change = entry.get("change_pct", 0)
                gap = entry.get("gap_pct", 0)
                # "Held gap" = still up at least 60% of the gap size
                if gap > 0 and change >= gap * 0.6:
                    entry["priority"] = gap * 2
                    entry["session_reason"] = f"Gap held +{change:.1f}% (gap was +{gap:.1f}%)"
                    candidates.append(entry)
                    seen.add(sym)

            # Stocks making new highs on accelerating volume (3x bar avg)
            for entry in movers:
                sym = entry["symbol"]
                if sym in seen:
                    continue
                rvol = entry.get("rvol", 0)
                change = entry.get("change_pct", 0)
                if rvol >= 2.0 and change >= 3.0:
                    entry["priority"] = change * rvol
                    entry["session_reason"] = f"New high +{change:.1f}% RVOL {rvol:.1f}x"
                    candidates.append(entry)
                    seen.add(sym)

            # Halt candidates / explosive runners: >7% movers
            for entry in runners:
                sym = entry["symbol"]
                if sym in seen:
                    continue
                change = entry.get("change_pct", 0)
                if change >= 7.0:
                    entry["priority"] = change * 1.5
                    entry["session_reason"] = f"Halt candidate +{change:.1f}%"
                    candidates.append(entry)
                    seen.add(sym)

        elif session == "postmarket":
            # Post-market: >3% on >300K volume
            for entry in movers + runners:
                change = abs(entry.get("change_pct", 0))
                volume = entry.get("volume", 0)
                if change >= 3.0 and volume >= 300_000:
                    entry["priority"] = change
                    entry["session_reason"] = f"After-hours +{change:.1f}% vol {volume/1e3:.0f}K"
                    candidates.append(entry)

        # Deduplicate and sort by priority
        seen_syms = set()
        unique = []
        for c in sorted(candidates, key=lambda x: x.get("priority", 0), reverse=True):
            sym = c["symbol"]
            if sym not in seen_syms:
                seen_syms.add(sym)
                unique.append(c)

        return unique

    def get_sector_momentum(self):
        """Get count of running stocks per sector.

        Used for sympathy play detection: if 3+ stocks in the same sector
        are running, the laggard is a sympathy play candidate.

        Returns:
            dict: {sector: count_of_runners}
        """
        sector_counts = {}
        for entry in self._cached_runners + self._cached_movers:
            sector = entry.get("sector", "Unknown")
            if sector and sector != "Unknown":
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
        return sector_counts

    def get_sector_performance(self):
        """Get average change % per sector from all scanned stocks.

        Used by regime detector for geopolitical rotation detection:
        when Energy is +5% and Travel is -5%, that's a geopolitical regime.

        Returns:
            dict: {sector: avg_change_pct}
        """
        sector_changes = {}  # {sector: [change1, change2, ...]}
        for entry in self._cached_movers + self._cached_runners:
            sector = entry.get("sector", "Unknown")
            if sector and sector != "Unknown":
                change = entry.get("change_pct", 0)
                if sector not in sector_changes:
                    sector_changes[sector] = []
                sector_changes[sector].append(change)

        # Also include losers from price cache for a fuller picture
        for sym, data in self._price_cache.items():
            sector = self.get_sector(sym)
            if sector and sector != "Unknown" and data.get("change_pct", 0) != 0:
                change = data["change_pct"]
                # Only include stocks with meaningful moves
                if abs(change) >= 1.0 and data.get("volume", 0) >= 50000:
                    if sector not in sector_changes:
                        sector_changes[sector] = []
                    sector_changes[sector].append(change)

        # Calculate averages
        result = {}
        for sector, changes in sector_changes.items():
            if len(changes) >= 2:  # Need at least 2 stocks for a meaningful average
                import numpy as np
                result[sector] = round(float(np.mean(changes)), 2)

        return result

    def get_sympathy_candidates(self):
        """Find sympathy play candidates.

        When 3+ stocks in the same sector are running, find the laggard
        in that sector (lowest change_pct) as a sympathy play.

        Returns:
            list of entry dicts for sympathy candidates
        """
        sector_momentum = self.get_sector_momentum()
        hot_sectors = {s for s, c in sector_momentum.items() if c >= 3}

        if not hot_sectors:
            return []

        # Group movers by hot sector
        sector_movers = {}
        for entry in self._cached_movers:
            sector = entry.get("sector", "Unknown")
            if sector in hot_sectors:
                if sector not in sector_movers:
                    sector_movers[sector] = []
                sector_movers[sector].append(entry)

        # Find laggards in each hot sector
        sympathy = []
        for sector, movers in sector_movers.items():
            if len(movers) >= 3:
                sorted_movers = sorted(movers, key=lambda x: x.get("change_pct", 0))
                # Laggard = lowest change in the sector
                laggard = sorted_movers[0]
                leader_change = sorted_movers[-1].get("change_pct", 0)
                laggard["session_reason"] = (
                    f"Sympathy: {sector} sector hot ({len(movers)} runners), "
                    f"laggard at +{laggard.get('change_pct', 0):.1f}% "
                    f"vs leader +{leader_change:.1f}%"
                )
                laggard["priority"] = leader_change - laggard.get("change_pct", 0)
                sympathy.append(laggard)

        return sympathy

    def get_losers(self, limit=100):
        """Get top losers from cached scan data ($0.50-$500 range)."""
        losers = []
        for sym, data in self._price_cache.items():
            if data["change_pct"] <= -2.0 and data["price"] >= 0.50 and data["price"] <= 500.0 and data["volume"] >= 30000:
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
