"""
Base strategy class - all strategies inherit from this.
"""
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

    def update_capital(self, new_capital):
        """Update allocated capital (called when account balance changes)."""
        self.allocated_capital = new_capital

    def _check_volume_filter(self, market_data, symbol, min_volume=None):
        """Check if symbol meets minimum volume requirement."""
        if min_volume is None:
            min_volume = self.config.get("min_volume", 500000)
        volume = market_data.get_volume(symbol)
        return volume is not None and volume >= min_volume
