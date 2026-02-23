"""
Polygon.io Full-Market Scanner
Scans ALL ~10,000 US stocks in a single API call.
Returns every stock with its real-time price, change %, volume, and pre-market data.

This replaces the narrow Alpaca top-50 screener with true full-market discovery.
One call to /v2/snapshot/locale/us/markets/stocks/tickers returns everything.

Free tier: 5 calls/min (one scan per cycle is plenty).
"""
import time
import requests
from bot.utils.logger import get_logger

log = get_logger("data.polygon_scanner")


class PolygonScanner:
    """
    Full-market scanner using Polygon.io snapshot API.
    Discovers ALL movers in real-time, not just a curated top-50.
    """

    SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"

    # Rate limit: free tier = 5/min, starter = 100/min
    MIN_INTERVAL = 15  # seconds between full scans (4/min, within free tier)

    def __init__(self, api_key):
        self.api_key = api_key
        self.enabled = bool(api_key)
        self._last_scan_time = 0
        self._cached_movers = []
        self._cached_runners = []
        self._cached_gap_ups = []

        if self.enabled:
            log.info("Polygon.io scanner ENABLED - full market scanning active")
        else:
            log.info("Polygon.io scanner disabled - set POLYGON_API_KEY to enable")

    def scan_full_market(self, min_change_pct=2.0, min_price=1.0, min_volume=50000):
        """
        Scan the entire US market for movers.
        Returns tuple: (movers, runners, gap_ups)

        movers: stocks with 2%+ change and decent volume
        runners: stocks with 10%+ change (explosive moves)
        gap_ups: stocks with 5%+ change (pre-market gap candidates)
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
                log.warning("Polygon rate limited - using cached results")
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

            for t in tickers:
                sym = t.get("ticker", "")
                if not sym or "." in sym or len(sym) > 5:
                    continue

                day = t.get("day", {})
                prev_day = t.get("prevDay", {})
                todaysChange = t.get("todaysChange", 0)
                todaysChangePerc = t.get("todaysChangePerc", 0)

                price = day.get("c", 0) or t.get("lastTrade", {}).get("p", 0)
                volume = day.get("v", 0) or 0
                prev_close = prev_day.get("c", 0) or 0
                prev_volume = prev_day.get("v", 1) or 1
                open_price = day.get("o", 0) or 0

                # Basic filters
                if price < min_price or price <= 0:
                    continue
                if volume < min_volume:
                    continue

                change_pct = todaysChangePerc or 0
                if abs(change_pct) < min_change_pct:
                    continue

                # Calculate RVOL
                rvol = round(volume / prev_volume, 1) if prev_volume > 0 else 0

                # Gap calculation (open vs prev close)
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

                # Categorize
                if change_pct >= 2.0:
                    movers.append(entry)

                if change_pct >= 10.0 and price <= 100.0:
                    runners.append(entry)

                if gap_pct >= 5.0:
                    gap_ups.append(entry)

            # Sort by change % descending
            movers.sort(key=lambda x: x["change_pct"], reverse=True)
            runners.sort(key=lambda x: x["change_pct"], reverse=True)
            gap_ups.sort(key=lambda x: x["gap_pct"], reverse=True)

            # Cache results
            self._cached_movers = movers
            self._cached_runners = runners
            self._cached_gap_ups = gap_ups

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
