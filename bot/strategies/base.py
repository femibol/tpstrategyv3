"""
Base strategy class - all strategies inherit from this.
"""
import time
from abc import ABC, abstractmethod
from bot.utils.logger import get_logger

log = get_logger("strategy.base")


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    def __init__(self, config, indicators, capital):
        self.config = config
        self.indicators = indicators
        self.allocated_capital = capital
        self.enabled = config.get("enabled", True)
        self.symbols = config.get("symbols", [])
        self.timeframe = config.get("timeframe", "5m")
        self.signals_generated = 0
        self.trades_taken = 0

        # Scanner: stores latest indicator values for EVERY symbol each cycle
        # This is what makes the analysis visible in the dashboard
        self.scan_results = {}  # symbol -> {indicators + verdict}

        # Dynamic symbol TTL tracking — prevents unbounded symbol accumulation
        self._dynamic_symbol_timestamps = {}  # symbol -> time.time() when last added/refreshed

    @abstractmethod
    def generate_signals(self, market_data):
        """
        Analyze market data and generate trading signals.

        Returns list of signal dicts:
        [{
            "symbol": "AAPL",
            "action": "buy" | "sell" | "short" | "cover",
            "price": 150.00,
            "stop_loss": 147.00,
            "take_profit": 156.00,
            "confidence": 0.75,  # 0-1
            "reason": "RSI oversold + bollinger bounce",
            "max_hold_bars": 20,
            "bar_seconds": 300,
        }]
        """
        pass

    def get_symbols(self):
        """Return symbols this strategy trades."""
        return self.symbols

    def get_scan_results(self):
        """Return latest scan results for all symbols (for dashboard)."""
        return self.scan_results

    def update_capital(self, new_capital):
        """Update allocated capital (called when account balance changes)."""
        self.allocated_capital = new_capital

    def prune_dynamic_symbols(self, max_age_seconds=1800):
        """Remove dynamic symbols not re-discovered within max_age_seconds (default 30 min).

        Symbols that are still actively moving get re-added every scan cycle,
        which refreshes their timestamp. Stale symbols that stopped moving
        are pruned to prevent unbounded accumulation.

        Returns number of symbols pruned.
        """
        if not hasattr(self, '_dynamic_symbols'):
            return 0
        now = time.time()
        stale = {
            sym for sym, ts in self._dynamic_symbol_timestamps.items()
            if now - ts > max_age_seconds
        }
        if not stale:
            return 0
        self._dynamic_symbols -= stale
        for sym in stale:
            self._dynamic_symbol_timestamps.pop(sym, None)
        return len(stale)

    def reset_dynamic_symbols(self):
        """Clear all dynamic symbols (called at start of each trading day)."""
        if hasattr(self, '_dynamic_symbols'):
            self._dynamic_symbols.clear()
        self._dynamic_symbol_timestamps.clear()

    def _check_volume_filter(self, market_data, symbol, min_volume=None):
        """Check if symbol meets minimum volume requirement."""
        if min_volume is None:
            min_volume = self.config.get("min_volume", 500000)
        volume = market_data.get_volume(symbol)
        return volume is not None and volume >= min_volume
