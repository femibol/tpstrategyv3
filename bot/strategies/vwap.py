"""
VWAP Scalping Strategy
- Trade around VWAP (Volume Weighted Average Price)
- Used by institutional desks for intraday execution
- Buy at VWAP support, sell at VWAP resistance
- Quick in-and-out trades
"""
import numpy as np
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.vwap")


class VWAPScalpStrategy(BaseStrategy):
    """
    VWAP Scalping - trade like institutional desks.

    Logic:
    1. Calculate intraday VWAP and standard deviation bands
    2. Buy when price dips to lower VWAP band with volume confirmation
    3. Sell when price returns to VWAP or hits upper band
    4. Quick trades - max 30 min hold
    5. Limit trades per day to avoid overtrading
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.vwap_bands = config.get("vwap_bands", 2)
        self.entry_band = config.get("entry_band", 1)
        self.min_distance = config.get("min_distance_from_vwap", 0.003)
        self.max_distance = config.get("max_distance_from_vwap", 0.015)
        self.vol_confirmation = config.get("volume_confirmation", True)
        self.max_trades = config.get("max_trades_per_day", 6)
        self.max_hold_minutes = config.get("max_holding_minutes", 30)
        self.trades_today = 0

    def generate_signals(self, market_data):
        signals = []

        # Check daily trade limit
        if self.trades_today >= self.max_trades:
            return signals

        for symbol in self.symbols:
            try:
                sig = self._analyze_symbol(symbol, market_data)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.debug(f"Error analyzing {symbol}: {e}")

        return signals

    def _analyze_symbol(self, symbol, market_data):
        """Analyze symbol relative to VWAP."""
        bars = market_data.get_bars(symbol, 78)  # Full trading day of 1-min bars
        if bars is None or len(bars) < 20:
            return None

        closes = bars["close"].values
        highs = bars["high"].values
        lows = bars["low"].values
        volumes = bars["volume"].values
        current_price = closes[-1]

        # Calculate VWAP
        typical_price = (highs + lows + closes) / 3
        cumulative_tp_vol = np.cumsum(typical_price * volumes)
        cumulative_vol = np.cumsum(volumes)

        vwap = cumulative_tp_vol[-1] / cumulative_vol[-1] if cumulative_vol[-1] > 0 else current_price

        # VWAP standard deviation bands
        vwap_array = cumulative_tp_vol / np.where(cumulative_vol > 0, cumulative_vol, 1)
        squared_diff = (typical_price - vwap_array) ** 2
        variance = np.cumsum(squared_diff * volumes) / np.where(cumulative_vol > 0, cumulative_vol, 1)
        vwap_std = np.sqrt(np.maximum(variance[-1], 0))

        if vwap_std == 0:
            return None

        upper_band_1 = vwap + vwap_std
        lower_band_1 = vwap - vwap_std
        upper_band_2 = vwap + 2 * vwap_std
        lower_band_2 = vwap - 2 * vwap_std

        # Distance from VWAP
        distance_pct = abs(current_price - vwap) / vwap

        # Volume check
        avg_vol = np.mean(volumes[-20:])
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        # --- BUY at lower VWAP band ---
        if (current_price <= lower_band_1 and
                self.min_distance <= distance_pct <= self.max_distance):

            confidence = 0.5

            # At second band = stronger signal
            if current_price <= lower_band_2:
                confidence += 0.2

            # Volume spike confirmation
            if vol_ratio > 1.3:
                confidence += 0.15

            # Price showing bounce (current > low of bar)
            if current_price > lows[-1]:
                confidence += 0.1

            confidence = min(1.0, confidence)

            # Tight stops for scalps
            stop_loss = lower_band_2 * 0.998  # Just below 2nd band
            take_profit = vwap  # Target VWAP

            signal = {
                "symbol": symbol,
                "action": "buy",
                "price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence": confidence,
                "reason": (
                    f"VWAP scalp BUY: Price at lower band, "
                    f"dist={distance_pct:.3f}, Vol={vol_ratio:.1f}x, "
                    f"VWAP=${vwap:.2f}"
                ),
                "max_hold_bars": self.max_hold_minutes,
                "bar_seconds": 60,  # 1-min bars
            }

            log.info(f"SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f}")
            self.signals_generated += 1
            self.trades_today += 1
            return signal

        # --- SHORT at upper VWAP band ---
        if (current_price >= upper_band_1 and
                self.min_distance <= distance_pct <= self.max_distance):

            confidence = 0.5

            if current_price >= upper_band_2:
                confidence += 0.2

            if vol_ratio > 1.3:
                confidence += 0.15

            confidence = min(1.0, confidence)

            stop_loss = upper_band_2 * 1.002
            take_profit = vwap

            signal = {
                "symbol": symbol,
                "action": "sell",
                "price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence": confidence,
                "reason": (
                    f"VWAP scalp SELL: Price at upper band, "
                    f"dist={distance_pct:.3f}, Vol={vol_ratio:.1f}x"
                ),
                "max_hold_bars": self.max_hold_minutes,
                "bar_seconds": 60,
            }

            log.info(f"SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f}")
            self.signals_generated += 1
            self.trades_today += 1
            return signal

        return None

    def reset_daily(self):
        """Reset daily trade counter."""
        self.trades_today = 0
