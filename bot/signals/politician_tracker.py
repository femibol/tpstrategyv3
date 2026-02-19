"""
Politician Trade Tracker - Follow congressional stock trades.

Monitors public filings from congress members (STOCK Act disclosures)
and generates trading signals to copy their trades.

Data sources:
- Capitol Trades API (capitoltrades.com)
- House/Senate financial disclosure filings
- Quiver Quantitative (quiverquant.com)

Key politicians tracked:
- Nancy Pelosi (known for exceptional returns on tech/options)
- Dan Crenshaw, Michael McCaul, Tommy Tuberville, etc.

Usage:
- Set CAPITOLTRADES_API_KEY in .env for premium data
- Or uses free scraping of public filings as fallback
"""
import json
import time
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

import requests

from bot.utils.logger import get_logger

log = get_logger("signals.politician_tracker")


# Well-known politicians and their trading patterns
TRACKED_POLITICIANS = {
    "nancy-pelosi": {
        "name": "Nancy Pelosi",
        "chamber": "House",
        "party": "D",
        "notable": "Tech calls (NVDA, AAPL, GOOGL, MSFT). Known for deep ITM LEAPS.",
        "priority": 1,  # Highest priority
    },
    "dan-crenshaw": {
        "name": "Dan Crenshaw",
        "chamber": "House",
        "party": "R",
        "notable": "Tech and defense stocks",
        "priority": 2,
    },
    "tommy-tuberville": {
        "name": "Tommy Tuberville",
        "chamber": "Senate",
        "party": "R",
        "notable": "High frequency trader. Tech, financials, energy.",
        "priority": 2,
    },
    "michael-mccaul": {
        "name": "Michael McCaul",
        "chamber": "House",
        "party": "R",
        "notable": "Tech and defense sector",
        "priority": 2,
    },
    "mark-green": {
        "name": "Mark Green",
        "chamber": "House",
        "party": "R",
        "notable": "Defense and healthcare",
        "priority": 3,
    },
    "josh-gottheimer": {
        "name": "Josh Gottheimer",
        "chamber": "House",
        "party": "D",
        "notable": "Tech and financials",
        "priority": 3,
    },
}


class PoliticianTradeTracker:
    """
    Tracks and generates signals from congressional trade disclosures.

    Flow:
    1. Poll for new disclosures (every 30 min)
    2. Parse trade details (ticker, direction, size, option details)
    3. Generate signal with appropriate conviction level
    4. Forward to engine for execution through broker chain
    """

    def __init__(self, config, callback=None):
        self.config = config
        self.callback = callback  # Called with signal dict when new trade found
        self.api_key = getattr(config, 'capitoltrades_api_key', '') or ''
        self.poll_interval = 1800  # 30 minutes
        self._running = False
        self._thread = None

        # Trade history to avoid duplicates - persisted to disk
        self._data_dir = Path(__file__).parent.parent.parent / "data"
        self._data_dir.mkdir(exist_ok=True)
        self._seen_file = self._data_dir / "politician_seen_trades.json"
        self.seen_trades = self._load_seen_trades()
        self.recent_disclosures = []
        self.signals_generated = []

        # Tracked politicians (configurable)
        self.tracked = TRACKED_POLITICIANS.copy()

        # Stats
        self.last_check = None
        self.total_signals = 0

    def _load_seen_trades(self):
        """Load seen trade IDs from disk (survive restarts)."""
        try:
            if self._seen_file.exists():
                with open(self._seen_file, "r") as f:
                    data = json.load(f)
                loaded = set(data) if isinstance(data, list) else set()
                log.info(f"Loaded {len(loaded)} seen politician trades from disk")
                return loaded
        except Exception as e:
            log.warning(f"Failed to load seen trades: {e}")
        return set()

    def _save_seen_trades(self):
        """Persist seen trade IDs to disk."""
        try:
            # Keep last 2000 entries to prevent unbounded growth
            trimmed = list(self.seen_trades)[-2000:]
            with open(self._seen_file, "w") as f:
                json.dump(trimmed, f)
        except Exception as e:
            log.warning(f"Failed to save seen trades: {e}")

    def start(self):
        """Start the tracker in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info(f"Politician trade tracker started ({len(self.seen_trades)} trades already seen)")

    def stop(self):
        """Stop the tracker."""
        self._running = False
        log.info("Politician trade tracker stopped")

    def _poll_loop(self):
        """Main polling loop."""
        # Initial check
        self._check_new_trades()

        while self._running:
            time.sleep(self.poll_interval)
            if self._running:
                self._check_new_trades()

    def _check_new_trades(self):
        """Check for new politician trade disclosures."""
        self.last_check = datetime.now().isoformat()
        log.info("Checking for new politician trade disclosures...")

        trades = []

        # Try Capitol Trades API first
        if self.api_key:
            trades = self._fetch_capitol_trades()

        # Fallback: scrape Quiver Quantitative
        if not trades:
            trades = self._fetch_quiver_trades()

        # Fallback: use House/Senate disclosure RSS
        if not trades:
            trades = self._fetch_disclosure_filings()

        if trades:
            new_count = 0
            for trade in trades:
                trade_id = self._trade_id(trade)
                if trade_id not in self.seen_trades:
                    self.seen_trades.add(trade_id)
                    self.recent_disclosures.append(trade)
                    new_count += 1

                    # Generate signal if it's actionable
                    signal = self._trade_to_signal(trade)
                    if signal:
                        self.signals_generated.append(signal)
                        self.total_signals += 1
                        if self.callback:
                            self.callback(signal)

            if new_count > 0:
                log.info(f"Found {new_count} new politician trades")
                self._save_seen_trades()  # Persist to disk immediately
            else:
                log.info("No new trades found")

            # Keep last 500 disclosures
            if len(self.recent_disclosures) > 500:
                self.recent_disclosures = self.recent_disclosures[-500:]

    def _fetch_capitol_trades(self):
        """Fetch trades from Capitol Trades API."""
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            url = "https://bff.capitoltrades.com/trades"
            params = {
                "page": 1,
                "pageSize": 50,
                "txDate": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
            }

            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                log.debug(f"Capitol Trades API returned {resp.status_code}")
                return []

            data = resp.json()
            trades = []

            for item in data.get("data", []):
                politician_id = item.get("politician", {}).get("slug", "")
                if politician_id not in self.tracked:
                    continue

                trade = {
                    "politician": item.get("politician", {}).get("name", "Unknown"),
                    "politician_id": politician_id,
                    "symbol": item.get("asset", {}).get("assetTicker", ""),
                    "asset_name": item.get("asset", {}).get("assetName", ""),
                    "action": item.get("txType", "").lower(),  # purchase, sale
                    "amount_range": item.get("txAmount", ""),
                    "tx_date": item.get("txDate", ""),
                    "disclosure_date": item.get("filingDate", ""),
                    "asset_type": item.get("asset", {}).get("assetType", "stock"),
                    "comment": item.get("comment", ""),
                    "source": "capitol_trades",
                }

                # Parse option details if present
                if "option" in trade["asset_type"].lower() or "call" in trade.get("comment", "").lower():
                    trade["is_option"] = True
                    trade["option_type"] = "call" if "call" in str(trade.get("comment", "")).lower() else "put"

                if trade["symbol"]:
                    trades.append(trade)

            return trades

        except Exception as e:
            log.debug(f"Capitol Trades API error: {e}")
            return []

    def _fetch_quiver_trades(self):
        """Fetch trades from Quiver Quantitative (free tier)."""
        try:
            url = "https://api.quiverquant.com/beta/live/congresstrading"
            headers = {"Accept": "application/json"}
            resp = requests.get(url, headers=headers, timeout=15)

            if resp.status_code != 200:
                log.debug(f"Quiver API returned {resp.status_code}")
                return []

            data = resp.json()
            trades = []

            for item in data[:100]:  # Limit to recent 100
                politician_name = item.get("Representative", "")
                politician_id = politician_name.lower().replace(" ", "-").replace(".", "")

                # Filter to tracked politicians
                if not any(politician_id.startswith(pid.split("-")[0]) for pid in self.tracked):
                    if not any(p["name"].lower() in politician_name.lower() for p in self.tracked.values()):
                        continue

                action_raw = item.get("Transaction", "").lower()
                if "purchase" in action_raw:
                    action = "purchase"
                elif "sale" in action_raw:
                    action = "sale"
                else:
                    continue

                trade = {
                    "politician": politician_name,
                    "politician_id": politician_id,
                    "symbol": item.get("Ticker", ""),
                    "asset_name": item.get("House", ""),
                    "action": action,
                    "amount_range": item.get("Range", ""),
                    "tx_date": item.get("TransactionDate", ""),
                    "disclosure_date": item.get("DisclosureDate", ""),
                    "asset_type": "stock",
                    "is_option": "option" in action_raw or "call" in action_raw or "put" in action_raw,
                    "source": "quiver",
                }

                if trade.get("is_option"):
                    trade["option_type"] = "call" if "call" in action_raw else "put"

                if trade["symbol"]:
                    trades.append(trade)

            return trades

        except Exception as e:
            log.debug(f"Quiver API error: {e}")
            return []

    def _fetch_disclosure_filings(self):
        """
        Fallback: Fetch from public disclosure RSS feeds.
        House: https://disclosures-clerk.house.gov/
        Senate: https://efdsearch.senate.gov/
        """
        trades = []

        # House disclosures
        try:
            url = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")

                for item in data:
                    tx_date = item.get("transaction_date", "")
                    if tx_date < cutoff:
                        continue

                    politician_name = item.get("representative", "")
                    politician_id = politician_name.lower().replace(" ", "-").replace("hon. ", "").replace(".", "")

                    # Check if tracked
                    is_tracked = False
                    for pid, pdata in self.tracked.items():
                        if pid in politician_id or pdata["name"].lower().split()[-1] in politician_name.lower():
                            is_tracked = True
                            politician_id = pid
                            break

                    if not is_tracked:
                        continue

                    action_raw = item.get("type", "").lower()
                    if "purchase" in action_raw:
                        action = "purchase"
                    elif "sale" in action_raw:
                        action = "sale"
                    else:
                        continue

                    trade = {
                        "politician": politician_name,
                        "politician_id": politician_id,
                        "symbol": item.get("ticker", "").replace("--", ""),
                        "asset_name": item.get("asset_description", ""),
                        "action": action,
                        "amount_range": item.get("amount", ""),
                        "tx_date": tx_date,
                        "disclosure_date": item.get("disclosure_date", ""),
                        "asset_type": "stock",
                        "source": "house_disclosures",
                    }

                    # Detect options from description
                    desc = item.get("asset_description", "").lower()
                    if "call" in desc or "put" in desc or "option" in desc:
                        trade["is_option"] = True
                        trade["option_type"] = "call" if "call" in desc else "put"

                    if trade["symbol"] and len(trade["symbol"]) <= 5:
                        trades.append(trade)

        except Exception as e:
            log.debug(f"House disclosures error: {e}")

        return trades

    def _trade_to_signal(self, trade):
        """Convert a politician trade disclosure to a trading signal."""
        symbol = trade["symbol"].upper().strip()
        if not symbol or len(symbol) > 5:
            return None

        action_raw = trade.get("action", "")
        if "purchase" in action_raw:
            action = "buy"
        elif "sale" in action_raw:
            action = "sell"
        else:
            return None

        # Determine conviction based on politician priority and amount
        politician_id = trade.get("politician_id", "")
        priority = 3
        for pid, pdata in self.tracked.items():
            if pid == politician_id or pdata["name"].lower() in trade.get("politician", "").lower():
                priority = pdata["priority"]
                break

        # Estimate confidence from priority + amount
        confidence = 0.5
        if priority == 1:
            confidence = 0.85  # Pelosi-tier
        elif priority == 2:
            confidence = 0.70
        else:
            confidence = 0.55

        # Boost for large amounts
        amount = trade.get("amount_range", "")
        if "$1,000,001" in amount or "$5,000,001" in amount or "$50,000,001" in amount:
            confidence = min(1.0, confidence + 0.1)
        elif "$500,001" in amount or "$250,001" in amount:
            confidence = min(1.0, confidence + 0.05)

        signal = {
            "symbol": symbol,
            "action": action,
            "confidence": confidence,
            "source": "politician_tracker",
            "strategy": "politician_copy",
            "reason": (
                f"Politician trade: {trade.get('politician', 'Unknown')} "
                f"{action_raw} {symbol} ({amount}) on {trade.get('tx_date', 'N/A')}"
            ),
            "politician": trade.get("politician", "Unknown"),
            "politician_id": politician_id,
            "amount_range": amount,
            "tx_date": trade.get("tx_date", ""),
            "disclosure_date": trade.get("disclosure_date", ""),
            "is_option": trade.get("is_option", False),
            "option_type": trade.get("option_type", ""),
            "asset_name": trade.get("asset_name", ""),
        }

        # For exit signals, don't require stop loss
        if action == "sell":
            signal["source"] = "exit"

        log.info(
            f"POLITICIAN SIGNAL: {trade.get('politician')} "
            f"{action.upper()} {symbol} | Amount: {amount} | "
            f"Confidence: {confidence:.0%}"
        )

        return signal

    def _trade_id(self, trade):
        """Generate unique ID for a trade to avoid duplicates."""
        return f"{trade.get('politician_id', '')}-{trade.get('symbol', '')}-{trade.get('action', '')}-{trade.get('tx_date', '')}"

    def get_recent_disclosures(self, limit=50):
        """Get recent disclosures for dashboard."""
        return self.recent_disclosures[-limit:]

    def get_signals(self, limit=20):
        """Get recent generated signals."""
        return self.signals_generated[-limit:]

    def get_status(self):
        """Get tracker status for dashboard."""
        return {
            "running": self._running,
            "last_check": self.last_check,
            "total_disclosures": len(self.recent_disclosures),
            "total_signals": self.total_signals,
            "tracked_politicians": {
                pid: pdata["name"] for pid, pdata in self.tracked.items()
            },
            "seen_trades": len(self.seen_trades),
        }

    def add_politician(self, politician_id, name, chamber="House", party="", priority=3, notable=""):
        """Add a politician to track."""
        self.tracked[politician_id] = {
            "name": name,
            "chamber": chamber,
            "party": party,
            "notable": notable,
            "priority": priority,
        }
        log.info(f"Now tracking: {name} ({politician_id})")

    def manual_check(self):
        """Manually trigger a check for new trades."""
        self._check_new_trades()
        return self.get_recent_disclosures(10)
