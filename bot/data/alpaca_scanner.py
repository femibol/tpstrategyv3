"""
Alpaca Markets Full-Market Data Provider (Premium SIP)
Primary data source for scanning, real-time prices, and historical bars.

Uses Alpaca's data API endpoints:
  - /v1beta1/screener/stocks/movers → Top gainers/losers (market movers)
  - /v1beta1/screener/stocks/most-actives → Highest volume stocks
  - /v2/stocks/snapshots → Real-time price snapshots (SIP consolidated)
  - /v2/stocks/{symbol}/bars → Historical OHLCV bars with pagination

Premium tier: unlimited SIP data, screener access, no throttling.
Replaces Polygon.io ($199/mo) with Alpaca ($99/mo) — same SIP feed.
"""
import time
from datetime import datetime, timedelta

import requests as _requests
import pandas as pd

from bot.utils.logger import get_logger

log = get_logger("data.alpaca_scanner")

DATA_BASE_URL = "https://data.alpaca.markets"


class AlpacaScanner:
    """
    Full-market data provider using Alpaca's data API.
    Handles scanning, real-time prices, and historical bars.
    Drop-in replacement for PolygonScanner.
    """

    # Seconds between full scans (screener + snapshots)
    MIN_INTERVAL = 15

    def __init__(self, api_key, secret_key):
        self.api_key = api_key
        self.secret_key = secret_key
        self.enabled = bool(api_key and secret_key)
        self._headers = {
            "APCA-API-KEY-ID": api_key or "",
            "APCA-API-SECRET-KEY": secret_key or "",
            "Accept": "application/json",
        }
        self._last_scan_time = 0
        self._cached_movers = []
        self._cached_runners = []
        self._cached_gap_ups = []

        # Price cache from snapshots — {symbol: {price, prev_close, volume, change_pct, open}}
        self._price_cache = {}
        self._price_cache_time = 0

        # Bars rate limiting (premium allows ~200 req/min, budget 60 for bars)
        self._bars_call_times = []

        # Float data cache — {symbol: {"float": int, "shares_outstanding": int, "fetched": timestamp}}
        self._float_cache = {}

        # Sector cache — {symbol: "Technology"}
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
            log.info("Alpaca data ENABLED (premium SIP) — primary data source")
        else:
            log.info("Alpaca data disabled — set ALPACA_API_KEY + ALPACA_SECRET_KEY")

    # =========================================================================
    # HTTP Helper
    # =========================================================================

    def _api_get(self, path, params=None):
        """Make authenticated GET request to Alpaca data API."""
        url = f"{DATA_BASE_URL}{path}"
        try:
            resp = _requests.get(url, headers=self._headers, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                log.warning("Alpaca data rate limited — using cached results")
                return None
            else:
                log.debug(f"Alpaca API {path}: status {resp.status_code}")
                return None
        except _requests.exceptions.Timeout:
            log.debug(f"Alpaca API timeout: {path}")
            return None
        except Exception as e:
            log.debug(f"Alpaca API error {path}: {e}")
            return None

    # =========================================================================
    # Full-Market Snapshot (scanning + price cache)
    # =========================================================================

    def scan_full_market(self, min_change_pct=2.0, min_price=0.50, max_price=100.0, min_volume=30000):
        """
        Scan the market for movers using Alpaca screener + snapshots.

        Discovery flow:
        1. GET /v1beta1/screener/stocks/movers → top 50 gainers + 50 losers
        2. GET /v1beta1/screener/stocks/most-actives → top 100 by volume
        3. GET /v2/stocks/snapshots for all discovered symbols → full price data
        4. Process into movers (2%+), runners (10%+), gap_ups (5%+)

        Returns tuple: (movers, runners, gap_ups)
        """
        if not self.enabled:
            return [], [], []

        # Rate limit
        now = time.time()
        if now - self._last_scan_time < self.MIN_INTERVAL:
            return self._cached_movers, self._cached_runners, self._cached_gap_ups

        try:
            # 1. Get top movers (gainers + losers)
            movers_resp = self._api_get("/v1beta1/screener/stocks/movers", {"top": 50})

            # 2. Get most active by volume
            actives_resp = self._api_get("/v1beta1/screener/stocks/most-actives", {
                "by": "volume", "top": 100,
            })

            # 3. Collect all discovered symbols
            all_symbols = set()

            if movers_resp:
                for g in movers_resp.get("gainers", []):
                    sym = g.get("symbol", "")
                    if sym and "." not in sym and len(sym) <= 5:
                        all_symbols.add(sym)
                for l in movers_resp.get("losers", []):
                    sym = l.get("symbol", "")
                    if sym and "." not in sym and len(sym) <= 5:
                        all_symbols.add(sym)

            if actives_resp:
                for a in actives_resp.get("most_actives", []):
                    sym = a.get("symbol", "")
                    if sym and "." not in sym and len(sym) <= 5:
                        all_symbols.add(sym)

            if not all_symbols:
                log.warning("Alpaca scan: no symbols discovered from movers/actives")
                return self._cached_movers, self._cached_runners, self._cached_gap_ups

            # 4. Get snapshots for all discovered symbols (batched)
            snapshots = self._get_snapshots_multi(list(all_symbols))
            self._last_scan_time = time.time()

            if not snapshots:
                log.warning("Alpaca scan: no snapshots returned")
                return self._cached_movers, self._cached_runners, self._cached_gap_ups

            # 5. Process snapshots into movers, runners, gap_ups
            movers = []
            runners = []
            gap_ups = []
            price_cache = {}
            ticker_count = 0

            for sym, snap in snapshots.items():
                daily = snap.get("dailyBar") or {}
                prev_daily = snap.get("prevDailyBar") or {}
                latest_trade = snap.get("latestTrade") or {}

                price = daily.get("c", 0) or 0
                volume = daily.get("v", 0) or 0
                open_price = daily.get("o", 0) or 0
                prev_close = prev_daily.get("c", 0) or 0
                prev_volume = prev_daily.get("v", 1) or 1

                # Fallback to latest trade price if daily bar is stale
                if price <= 0:
                    price = latest_trade.get("p", 0) or 0

                if price <= 0:
                    continue

                ticker_count += 1

                change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                gap_pct = round((open_price - prev_close) / prev_close * 100, 2) if prev_close > 0 and open_price > 0 else 0
                rvol = round(volume / prev_volume, 1) if prev_volume > 0 else 0

                # Cache price for ALL valid tickers (used for real-time quotes)
                price_cache[sym] = {
                    "price": round(price, 2),
                    "prev_close": round(prev_close, 2),
                    "volume": int(volume),
                    "change_pct": change_pct,
                    "open": round(open_price, 2),
                }

                # Apply mover filters
                if price < min_price:
                    continue
                if max_price and price > max_price:
                    continue
                if volume < min_volume:
                    continue
                if abs(change_pct) < min_change_pct:
                    continue

                entry = {
                    "symbol": sym,
                    "name": sym,
                    "price": round(price, 2),
                    "change_pct": change_pct,
                    "volume": int(volume),
                    "avg_volume": int(prev_volume),
                    "rvol": rvol,
                    "gap_pct": gap_pct,
                    "prev_close": round(prev_close, 2),
                    "open": round(open_price, 2),
                    "market_cap": 0,
                    "float_shares": self._float_cache.get(sym, {}).get("float", 0),
                    "sector": self.get_sector(sym),
                    "source": "alpaca",
                }

                if change_pct >= 2.0:
                    movers.append(entry)
                if change_pct >= 10.0:
                    runners.append(entry)
                if gap_pct >= 5.0:
                    gap_ups.append(entry)

            movers.sort(key=lambda x: x["change_pct"], reverse=True)
            runners.sort(key=lambda x: x["change_pct"], reverse=True)
            gap_ups.sort(key=lambda x: x["gap_pct"], reverse=True)

            self._cached_movers = movers
            self._cached_runners = runners
            self._cached_gap_ups = gap_ups
            self._price_cache.update(price_cache)
            self._price_cache_time = time.time()

            log.info(
                f"Alpaca scan: {ticker_count} tickers | "
                f"{len(movers)} movers (2%+) | {len(runners)} runners (10%+) | "
                f"{len(gap_ups)} gap-ups (5%+)"
            )

            return movers, runners, gap_ups

        except Exception as e:
            log.warning(f"Alpaca scan error: {e}")
            return self._cached_movers, self._cached_runners, self._cached_gap_ups

    def refresh_snapshots(self, symbols):
        """Refresh price cache for specific symbols (e.g. positions, watchlist).
        Called by the engine to keep prices fresh for symbols not in the screener."""
        if not self.enabled or not symbols:
            return

        snapshots = self._get_snapshots_multi(symbols)
        if not snapshots:
            return

        for sym, snap in snapshots.items():
            daily = snap.get("dailyBar") or {}
            prev_daily = snap.get("prevDailyBar") or {}
            latest_trade = snap.get("latestTrade") or {}

            price = daily.get("c", 0) or latest_trade.get("p", 0) or 0
            if price <= 0:
                continue

            prev_close = prev_daily.get("c", 0) or 0
            volume = daily.get("v", 0) or 0
            open_price = daily.get("o", 0) or 0
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

            self._price_cache[sym] = {
                "price": round(price, 2),
                "prev_close": round(prev_close, 2),
                "volume": int(volume),
                "change_pct": change_pct,
                "open": round(open_price, 2),
            }

        self._price_cache_time = time.time()

    def _get_snapshots_multi(self, symbols):
        """Get snapshots for multiple symbols (batched in groups of 200)."""
        all_snapshots = {}
        batch_size = 200

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            symbols_str = ",".join(batch)
            data = self._api_get("/v2/stocks/snapshots", {
                "symbols": symbols_str,
                "feed": "sip",
            })
            if data:
                all_snapshots.update(data)

        return all_snapshots

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
            "source": "ALPACA",
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
    # Historical Bars (Alpaca Bars API)
    # =========================================================================

    def _can_make_bar_call(self):
        """Check if we can make another API call within rate limits.
        Premium tier allows ~200 req/min — budget 60 for bars."""
        now = time.time()
        self._bars_call_times = [t for t in self._bars_call_times if now - t < 60]
        return len(self._bars_call_times) < 60

    def fetch_bars(self, symbol, bar_size="5 mins", lookback_days=30):
        """
        Fetch historical OHLCV bars from Alpaca bars API.
        Uses SIP consolidated data with split adjustment.
        Paginates automatically for large date ranges.

        Returns pandas DataFrame with columns: open, high, low, close, volume
        """
        if not self.enabled:
            return None

        if not self._can_make_bar_call():
            log.debug(f"Alpaca bars rate limited, skipping {symbol}")
            return None

        interval_map = {
            "1 min": "1Min",
            "5 mins": "5Min",
            "15 mins": "15Min",
            "30 mins": "30Min",
            "1 hour": "1Hour",
            "1 day": "1Day",
        }
        timeframe = interval_map.get(bar_size, "5Min")

        end_dt = datetime.utcnow()
        start_dt = end_dt - timedelta(days=lookback_days)

        try:
            self._bars_call_times.append(time.time())

            all_bars = []
            page_token = None

            while True:
                params = {
                    "timeframe": timeframe,
                    "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": 10000,
                    "adjustment": "split",
                    "feed": "sip",
                    "sort": "asc",
                }
                if page_token:
                    params["page_token"] = page_token

                data = self._api_get(f"/v2/stocks/{symbol}/bars", params)
                if not data:
                    break

                bars = data.get("bars") or []
                if not bars:
                    break

                all_bars.extend(bars)

                page_token = data.get("next_page_token")
                if not page_token:
                    break

                # Track additional pages as API calls
                self._bars_call_times.append(time.time())

            if not all_bars:
                return None

            rows = []
            for b in all_bars:
                rows.append({
                    "timestamp": b["t"],
                    "open": b["o"],
                    "high": b["h"],
                    "low": b["l"],
                    "close": b["c"],
                    "volume": b["v"],
                })

            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["timestamp"])
            df = df[["open", "high", "low", "close", "volume"]]
            df = df.dropna(subset=["close"])
            return df if not df.empty else None

        except Exception as e:
            log.debug(f"Alpaca bars error for {symbol}: {e}")
            return None

    # =========================================================================
    # Convenience Methods (same interface as PolygonScanner)
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
        """Get float estimate for a symbol. Uses cached data if available."""
        if symbol in self._float_cache:
            return self._float_cache[symbol].get("float", 0)
        return 0

    def _fetch_float_batch(self, symbols, max_fetches=2):
        """Fetch float data from Yahoo Finance (free).
        Polygon used to provide this via ticker_details — Yahoo is the free replacement."""
        try:
            import yfinance as yf
        except ImportError:
            return

        fetched = 0
        for sym in symbols:
            if sym in self._float_cache:
                continue
            if fetched >= max_fetches:
                break

            try:
                ticker = yf.Ticker(sym)
                info = ticker.info or {}
                float_shares = info.get("floatShares", 0) or 0
                shares_out = info.get("sharesOutstanding", 0) or 0
                sector = info.get("sector", "Unknown") or "Unknown"
                self._float_cache[sym] = {
                    "float": int(float_shares),
                    "shares_outstanding": int(shares_out),
                    "sector": sector,
                    "fetched": time.time(),
                }
                if sector != "Unknown":
                    self._sector_cache[sym] = sector
                fetched += 1
                if float_shares > 0:
                    log.debug(f"Float data for {sym}: {float_shares / 1e6:.1f}M shares (Yahoo)")
            except Exception as e:
                log.debug(f"Float fetch failed for {sym}: {e}")
                self._float_cache[sym] = {
                    "float": 0, "shares_outstanding": 0,
                    "sector": "Unknown", "fetched": time.time(),
                }
                fetched += 1

    def get_sector(self, symbol):
        """Get sector for a symbol. Checks local cache first, then known mappings."""
        sym = symbol.upper()
        if sym in self._sector_cache:
            return self._sector_cache[sym]
        if sym in self._known_sectors:
            return self._known_sectors[sym]
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
        """Get top losers from cached scan data."""
        losers = []
        for sym, data in self._price_cache.items():
            if (data["change_pct"] <= -2.0 and data["price"] >= 0.50
                    and data["price"] <= 100.0 and data["volume"] >= 30000):
                losers.append({
                    "symbol": sym,
                    "name": sym,
                    "price": data["price"],
                    "change_pct": data["change_pct"],
                    "volume": data["volume"],
                    "source": "alpaca",
                })
        losers.sort(key=lambda x: x["change_pct"])
        return losers[:limit]
