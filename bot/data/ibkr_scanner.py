"""
IBKR Market Data Scanner — uses Interactive Brokers for scanning + prices + bars.

Drop-in replacement for AlpacaScanner when IBKR (TWS/IB Gateway) is connected.
Uses ib_async (successor to ib_insync) for:
  - reqScannerSubscription() → Top gainers, losers, most-actives
  - reqMktData() / reqMarketDataType() → Real-time price snapshots
  - reqHistoricalData() → Historical OHLCV bars
  - get_live_price() → Streaming prices from broker's existing subscriptions

Requires TWS or IB Gateway running locally. Zero additional cost.
Falls back gracefully if IBKR is not connected.
"""
import time
import threading
from datetime import datetime, timedelta

import pandas as pd

from bot.utils.logger import get_logger

log = get_logger("data.ibkr_scanner")

try:
    from ib_async import Stock, ScannerSubscription, TagValue
    HAS_IB = True
except ImportError:
    try:
        from ib_insync import Stock, ScannerSubscription, TagValue
        HAS_IB = True
    except ImportError:
        HAS_IB = False


class IBKRScanner:
    """
    Full-market data provider using IBKR's TWS API.
    Same interface as AlpacaScanner — drop-in replacement.

    Provides:
    - scan_full_market() → movers, runners, gap_ups
    - get_price() / get_all_prices() → cached real-time prices
    - get_snapshot() / get_snapshots_batch() → price snapshots
    - fetch_bars() → historical OHLCV bars
    - get_top_movers() / get_runners() / get_gap_ups()
    """

    MIN_INTERVAL = 15  # Seconds between full scans

    def __init__(self, broker):
        """
        Args:
            broker: IBKRBroker instance (must be connected)
        """
        self.broker = broker
        self.enabled = False
        self._last_scan_time = 0
        self._cached_movers = []
        self._cached_runners = []
        self._cached_gap_ups = []

        # Price cache — {symbol: {price, prev_close, volume, change_pct, open}}
        self._price_cache = {}
        self._price_cache_time = 0
        self._cache_lock = threading.Lock()

        # Scanner subscription handles
        self._scanner_subs = {}

        # Float/sector caches (same as AlpacaScanner for compatibility)
        self._float_cache = {}
        self._sector_cache = {}
        self._known_sectors = {
            "SOFI": "Financials", "HOOD": "Financials", "AFRM": "Financials",
            "UPST": "Financials", "COIN": "Financials", "PYPL": "Financials",
            "SQ": "Financials",
            "IONQ": "Technology", "RGTI": "Technology", "AI": "Technology",
            "BBAI": "Technology", "SOUN": "Technology", "PLTR": "Technology",
            "MARA": "Crypto", "RIOT": "Crypto", "CLSK": "Crypto",
            "CIFR": "Crypto", "MSTR": "Crypto",
            "RIVN": "EV/Clean", "LCID": "EV/Clean", "NIO": "EV/Clean",
            "PLUG": "EV/Clean", "CHPT": "EV/Clean", "ENPH": "EV/Clean",
            "FSLR": "EV/Clean", "QS": "EV/Clean",
            "DNA": "Healthcare", "MRNA": "Healthcare", "HIMS": "Healthcare",
            "RKLB": "Aerospace", "LUNR": "Aerospace", "ASTS": "Aerospace",
            "JOBY": "Aerospace", "SPCE": "Aerospace",
            "GME": "Consumer", "AMC": "Consumer", "WISH": "Consumer",
            "OPEN": "Real Estate", "SNAP": "Technology", "RBLX": "Technology",
            "SKLZ": "Technology", "GSAT": "Technology",
        }

        # Bars rate limiting (IBKR pacing: ~60 requests per 10 min)
        self._bars_call_times = []

        self._check_connection()

    def _check_connection(self):
        """Verify IBKR connection and enable scanner."""
        if not HAS_IB:
            log.warning("ib_async not installed — IBKRScanner disabled")
            return
        if self.broker and self.broker.is_connected():
            self.enabled = True
            log.info("IBKRScanner ENABLED — using IBKR for scanning + data (zero cost)")
        else:
            log.info("IBKRScanner disabled — IBKR not connected")

    @property
    def ib(self):
        """Shortcut to the broker's IB connection."""
        if self.broker and hasattr(self.broker, 'ib'):
            return self.broker.ib
        return None

    # =========================================================================
    # Full-Market Scan (IBKR Scanner Subscriptions)
    # =========================================================================

    def scan_full_market(self, min_change_pct=2.0, min_price=0.50, max_price=100.0, min_volume=30000):
        """
        Scan the market using IBKR's built-in scanner.

        Discovery flow:
        1. reqScannerSubscription(TOP_PERC_GAIN) → top gainers
        2. reqScannerSubscription(MOST_ACTIVE) → highest volume
        3. Fetch snapshots for discovered symbols via reqMktData
        4. Process into movers (2%+), runners (10%+), gap_ups (5%+)

        Returns tuple: (movers, runners, gap_ups)
        """
        if not self.enabled or not self.ib:
            return self._cached_movers, self._cached_runners, self._cached_gap_ups

        now = time.time()
        if now - self._last_scan_time < self.MIN_INTERVAL:
            return self._cached_movers, self._cached_runners, self._cached_gap_ups

        try:
            all_symbols = set()

            # 1. Top gainers
            gainers = self._run_scanner("TOP_PERC_GAIN", max_results=50,
                                        min_price=min_price, max_price=max_price)
            all_symbols.update(gainers)

            # 2. Top losers
            losers = self._run_scanner("TOP_PERC_LOSE", max_results=50,
                                       min_price=min_price, max_price=max_price)
            all_symbols.update(losers)

            # 3. Most active by volume
            actives = self._run_scanner("MOST_ACTIVE", max_results=100,
                                        min_price=min_price, max_price=max_price)
            all_symbols.update(actives)

            if not all_symbols:
                log.warning("IBKR scan: no symbols discovered from scanner")
                return self._cached_movers, self._cached_runners, self._cached_gap_ups

            # 4. Get snapshots for all discovered symbols
            snapshots = self._get_snapshots_batch(list(all_symbols))
            self._last_scan_time = time.time()

            if not snapshots:
                log.warning("IBKR scan: no snapshots returned")
                return self._cached_movers, self._cached_runners, self._cached_gap_ups

            # 5. Process into movers, runners, gap_ups
            movers = []
            runners = []
            gap_ups = []
            price_cache = {}
            ticker_count = 0

            for sym, snap in snapshots.items():
                price = snap.get("price", 0)
                prev_close = snap.get("prev_close", 0)
                volume = snap.get("volume", 0)
                open_price = snap.get("open", 0)

                if price <= 0:
                    continue

                ticker_count += 1

                change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                gap_pct = round((open_price - prev_close) / prev_close * 100, 2) if prev_close > 0 and open_price > 0 else 0

                # Estimate RVOL from volume (simplified — no prev_volume from scanner)
                rvol = 0

                # Cache price for ALL valid tickers
                price_cache[sym] = {
                    "price": round(price, 2),
                    "prev_close": round(prev_close, 2),
                    "volume": int(volume),
                    "change_pct": change_pct,
                    "open": round(open_price, 2) if open_price else 0,
                }

                # Apply filters
                if price < min_price or (max_price and price > max_price):
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
                    "avg_volume": 0,
                    "rvol": rvol,
                    "gap_pct": gap_pct,
                    "prev_close": round(prev_close, 2),
                    "open": round(open_price, 2) if open_price else 0,
                    "market_cap": 0,
                    "float_shares": self._float_cache.get(sym, {}).get("float", 0),
                    "sector": self.get_sector(sym),
                    "source": "ibkr",
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

            with self._cache_lock:
                self._price_cache.update(price_cache)
                self._price_cache_time = time.time()

            log.info(
                f"IBKR scan: {ticker_count} tickers | "
                f"{len(movers)} movers (2%+) | {len(runners)} runners (10%+) | "
                f"{len(gap_ups)} gap-ups (5%+)"
            )

            return movers, runners, gap_ups

        except Exception as e:
            log.warning(f"IBKR scan error: {e}")
            return self._cached_movers, self._cached_runners, self._cached_gap_ups

    def _run_scanner(self, scan_code, max_results=50, min_price=0.50, max_price=100.0):
        """
        Run an IBKR scanner subscription and return list of symbols.

        scan_code options:
        - TOP_PERC_GAIN: top % gainers
        - TOP_PERC_LOSE: top % losers
        - MOST_ACTIVE: highest volume
        - HOT_BY_VOLUME: unusual volume
        - HIGH_OPEN_GAP: biggest gap ups
        """
        if not self.ib:
            return []

        try:
            sub = ScannerSubscription(
                instrument="STK",
                locationCode="STK.US.MAJOR",
                scanCode=scan_code,
                numberOfRows=max_results,
            )

            # Price filters via tag values
            tag_values = [
                TagValue("priceAbove", str(min_price)),
            ]
            if max_price:
                tag_values.append(TagValue("priceBelow", str(max_price)))

            scan_results = self.ib.reqScannerData(sub, scannerSubscriptionFilterOptions=tag_values)

            symbols = []
            for result in scan_results:
                contract = result.contractDetails.contract
                sym = contract.symbol
                # Filter: US stocks only, no OTC, max 5 chars
                if sym and len(sym) <= 5 and "." not in sym:
                    symbols.append(sym)

            log.debug(f"IBKR scanner {scan_code}: {len(symbols)} symbols")
            return symbols

        except Exception as e:
            log.debug(f"IBKR scanner {scan_code} failed: {e}")
            return []

    # =========================================================================
    # Snapshots — Real-time price data from IBKR
    # =========================================================================

    def _get_snapshots_batch(self, symbols):
        """
        Get real-time snapshots for a batch of symbols via reqMktData.
        Uses frozen snapshots (snapshot=True) for efficiency.
        Returns {symbol: {price, prev_close, volume, open, ...}}
        """
        if not self.ib or not symbols:
            return {}

        snapshots = {}
        contracts = []
        sym_map = {}

        # Qualify contracts in batch
        for sym in symbols:
            contract = Stock(sym, "SMART", "USD")
            contracts.append(contract)
            sym_map[id(contract)] = sym

        try:
            qualified = self.ib.qualifyContracts(*contracts)
        except Exception as e:
            log.debug(f"Contract qualification failed: {e}")
            qualified = contracts

        # Request frozen market data type (for pre/post market)
        try:
            self.ib.reqMarketDataType(4)  # 4 = delayed-frozen (works outside market hours too)
        except Exception:
            pass

        # Request snapshots in batches to avoid overwhelming IBKR
        batch_size = 50
        for i in range(0, len(qualified), batch_size):
            batch = qualified[i:i + batch_size]
            tickers = []

            for contract in batch:
                try:
                    ticker = self.ib.reqMktData(contract, genericTickList="", snapshot=True)
                    tickers.append((contract.symbol, ticker))
                except Exception:
                    continue

            # Wait for snapshot data to arrive
            if tickers:
                self.ib.sleep(2)

            for sym, ticker in tickers:
                try:
                    # Extract price data from ticker
                    last = ticker.last if ticker.last and ticker.last > 0 else None
                    close = ticker.close if ticker.close and ticker.close > 0 else None
                    bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
                    ask = ticker.ask if ticker.ask and ticker.ask > 0 else None

                    # Best price: last trade, close, or midpoint
                    price = last or close
                    if not price and bid and ask:
                        price = (bid + ask) / 2

                    if not price or price <= 0:
                        continue

                    volume = int(ticker.volume) if ticker.volume and ticker.volume > 0 else 0
                    prev_close = close or 0
                    open_price = ticker.open if ticker.open and ticker.open > 0 else 0
                    high = ticker.high if ticker.high and ticker.high > 0 else price
                    low = ticker.low if ticker.low and ticker.low > 0 else price

                    snapshots[sym] = {
                        "price": round(price, 2),
                        "prev_close": round(prev_close, 2),
                        "volume": volume,
                        "open": round(open_price, 2),
                        "high": round(high, 2),
                        "low": round(low, 2),
                    }

                    # Cancel the snapshot request
                    self.ib.cancelMktData(ticker.contract)

                except Exception as e:
                    log.debug(f"Snapshot processing failed for {sym}: {e}")

        # Restore live market data type
        try:
            self.ib.reqMarketDataType(1)  # 1 = live
        except Exception:
            pass

        return snapshots

    def refresh_snapshots(self, symbols):
        """Refresh price cache for specific symbols (positions, watchlist).
        Uses streaming data from broker if available, falls back to snapshots."""
        if not self.enabled or not symbols:
            return

        updated = 0

        # First try streaming prices from broker (instant, no API call)
        for sym in symbols:
            if hasattr(self.broker, 'get_live_price'):
                live = self.broker.get_live_price(sym)
                if live and live.get("price"):
                    with self._cache_lock:
                        existing = self._price_cache.get(sym, {})
                        self._price_cache[sym] = {
                            "price": round(live["price"], 2),
                            "prev_close": existing.get("prev_close", live.get("close", 0) or 0),
                            "volume": live.get("volume", 0),
                            "change_pct": existing.get("change_pct", 0),
                            "open": existing.get("open", live.get("open", 0) or 0),
                        }
                    updated += 1

        # For symbols without streaming, get snapshots
        missing = [s for s in symbols if s not in self._price_cache]
        if missing:
            snap_data = self._get_snapshots_batch(missing)
            if snap_data:
                with self._cache_lock:
                    for sym, snap in snap_data.items():
                        price = snap.get("price", 0)
                        if price <= 0:
                            continue
                        prev_close = snap.get("prev_close", 0)
                        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                        self._price_cache[sym] = {
                            "price": round(price, 2),
                            "prev_close": round(prev_close, 2),
                            "volume": snap.get("volume", 0),
                            "change_pct": change_pct,
                            "open": snap.get("open", 0),
                        }
                        updated += 1

        if updated:
            with self._cache_lock:
                self._price_cache_time = time.time()

    # =========================================================================
    # Real-Time Prices (from cache — no extra API calls)
    # =========================================================================

    def get_price(self, symbol):
        """Get cached real-time price for a symbol."""
        with self._cache_lock:
            entry = self._price_cache.get(symbol)
        return entry["price"] if entry else None

    def get_all_prices(self):
        """Get {symbol: price} dict for ALL cached tickers."""
        with self._cache_lock:
            return {sym: d["price"] for sym, d in self._price_cache.items()}

    def get_snapshot(self, symbol):
        """Get full snapshot data for a symbol from cache."""
        with self._cache_lock:
            entry = self._price_cache.get(symbol)
        if not entry:
            return None
        return {
            "symbol": symbol,
            "price": entry["price"],
            "prev_close": entry["prev_close"],
            "change": round(entry["price"] - entry["prev_close"], 2) if entry["prev_close"] else 0,
            "change_pct": entry.get("change_pct", 0),
            "volume": entry["volume"],
            "source": "IBKR",
        }

    def get_snapshots_batch(self, symbols):
        """Get {symbol: price} for a batch of symbols from cache."""
        result = {}
        with self._cache_lock:
            for sym in symbols:
                entry = self._price_cache.get(sym)
                if entry and entry["price"] > 0:
                    result[sym] = entry["price"]
        return result

    @property
    def price_cache_age(self):
        """Seconds since last snapshot update."""
        with self._cache_lock:
            t = self._price_cache_time
        if t == 0:
            return float("inf")
        return time.time() - t

    # =========================================================================
    # Historical Bars (IBKR Historical Data)
    # =========================================================================

    def _can_make_bar_call(self):
        """Check IBKR pacing rules: ~60 requests per 10 minutes."""
        now = time.time()
        self._bars_call_times = [t for t in self._bars_call_times if now - t < 600]
        return len(self._bars_call_times) < 55

    def fetch_bars(self, symbol, bar_size="5 mins", lookback_days=30):
        """
        Fetch historical OHLCV bars from IBKR.

        Returns pandas DataFrame with columns: open, high, low, close, volume
        """
        if not self.enabled or not self.ib:
            return None

        if not self._can_make_bar_call():
            log.debug(f"IBKR bars pacing limited, skipping {symbol}")
            return None

        # Map bar sizes to IBKR format
        bar_map = {
            "1 min": "1 min",
            "5 mins": "5 mins",
            "15 mins": "15 mins",
            "30 mins": "30 mins",
            "1 hour": "1 hour",
            "1 day": "1 day",
        }
        ib_bar_size = bar_map.get(bar_size, "5 mins")

        try:
            self._bars_call_times.append(time.time())

            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=f"{lookback_days} D",
                barSizeSetting=ib_bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )

            if not bars:
                return None

            rows = []
            for b in bars:
                rows.append({
                    "timestamp": b.date,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                })

            df = pd.DataFrame(rows)
            if "timestamp" in df.columns:
                df.index = pd.to_datetime(df["timestamp"])
                df = df[["open", "high", "low", "close", "volume"]]
            df = df.dropna(subset=["close"])
            return df if not df.empty else None

        except Exception as e:
            log.debug(f"IBKR bars error for {symbol}: {e}")
            return None

    # =========================================================================
    # Convenience Methods (same interface as AlpacaScanner)
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
        """Get float estimate for a symbol."""
        if symbol in self._float_cache:
            return self._float_cache[symbol].get("float", 0)
        return 0

    def get_sector(self, symbol):
        """Get sector for a symbol."""
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
        """Get count of symbols per sector."""
        counts = {}
        for sym in symbols:
            sector = self.get_sector(sym)
            counts[sector] = counts.get(sector, 0) + 1
        return counts

    def get_losers(self, limit=100):
        """Get top losers from cached scan data."""
        losers = []
        with self._cache_lock:
            for sym, data in self._price_cache.items():
                if (data["change_pct"] <= -2.0 and data["price"] >= 0.50
                        and data["price"] <= 100.0 and data["volume"] >= 30000):
                    losers.append({
                        "symbol": sym,
                        "name": sym,
                        "price": data["price"],
                        "change_pct": data["change_pct"],
                        "volume": data["volume"],
                        "source": "ibkr",
                    })
        losers.sort(key=lambda x: x["change_pct"])
        return losers[:limit]
