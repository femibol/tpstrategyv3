"""
Mean Reversion Strategy
- Buy when price drops significantly below its mean (oversold)
- Sell when price returns to mean
- Uses RSI, Bollinger Bands, and Z-Score
- Best for range-bound markets with liquid stocks
"""
import numpy as np
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.mean_reversion")


class MeanReversionStrategy(BaseStrategy):
    """
    Mean Reversion - the bread and butter for small accounts.

    Logic:
    1. Calculate Z-score of price relative to moving average
    2. Check RSI for oversold/overbought
    3. Check if price is at Bollinger Band extremes
    4. Enter when multiple indicators confirm oversold
    5. Exit when price reverts to mean
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.lookback = config.get("lookback_period", 20)
        self.entry_zscore = config.get("entry_zscore", -2.0)
        self.exit_zscore = config.get("exit_zscore", 0.0)
        self.rsi_oversold = config.get("rsi_oversold", 30)
        self.rsi_overbought = config.get("rsi_overbought", 70)
        self.bb_period = config.get("bollinger_period", 20)
        self.bb_std = config.get("bollinger_std", 2.0)
        self.max_hold = config.get("max_holding_periods", 20)

    def generate_signals(self, market_data):
        signals = []

        for symbol in self.symbols:
            try:
                sig = self._analyze_symbol(symbol, market_data)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.debug(f"Error analyzing {symbol}: {e}")

        return signals

    def _analyze_symbol(self, symbol, market_data):
        """Analyze a single symbol for mean reversion entry/exit."""
        bars = market_data.get_bars(symbol, self.lookback + 10)
        if bars is None or len(bars) < self.lookback:
            return None

        closes = bars["close"].values
        volumes = bars["volume"].values
        current_price = closes[-1]

        # Calculate indicators
        sma = np.mean(closes[-self.lookback:])
        std = np.std(closes[-self.lookback:])

        if std == 0:
            return None

        # Z-Score: how many std devs from mean
        zscore = (current_price - sma) / std

        # RSI
        rsi = self.indicators.rsi(closes, period=14)

        # Bollinger Bands
        bb_upper = sma + (self.bb_std * std)
        bb_lower = sma - (self.bb_std * std)

        # Volume check
        avg_vol = np.mean(volumes[-self.lookback:])
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        # --- BUY Signal (Oversold) ---
        if zscore <= self.entry_zscore and rsi < self.rsi_oversold:
            # Price is 2+ std devs below mean AND RSI is oversold
            confidence = min(1.0, abs(zscore) / 3.0 * 0.5 + (1 - rsi / 100) * 0.5)

            # Stronger signal if at lower Bollinger Band
            at_lower_bb = current_price <= bb_lower
            if at_lower_bb:
                confidence = min(1.0, confidence + 0.15)

            # Volume confirmation
            if vol_ratio > 1.3:
                confidence = min(1.0, confidence + 0.1)

            stop_loss = current_price * 0.97  # 3% stop
            take_profit = sma  # Target the mean

            signal = {
                "symbol": symbol,
                "action": "buy",
                "price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence": confidence,
                "reason": (
                    f"Mean reversion BUY: Z={zscore:.2f}, "
                    f"RSI={rsi:.0f}, BB={'lower' if at_lower_bb else 'near'}, "
                    f"Vol={vol_ratio:.1f}x"
                ),
                "max_hold_bars": self.max_hold,
                "bar_seconds": self._timeframe_to_seconds(),
            }

            log.info(f"SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f}")
            self.signals_generated += 1
            return signal

        # --- SELL Signal (Overbought - for existing positions) ---
        if zscore >= abs(self.entry_zscore) and rsi > self.rsi_overbought:
            signal = {
                "symbol": symbol,
                "action": "sell",
                "price": current_price,
                "confidence": min(1.0, zscore / 3.0),
                "reason": f"Mean reversion SELL: Z={zscore:.2f}, RSI={rsi:.0f}",
                "max_hold_bars": self.max_hold,
                "bar_seconds": self._timeframe_to_seconds(),
            }
            log.info(f"SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f}")
            return signal

        return None

    def _timeframe_to_seconds(self):
        tf = self.timeframe
        if "m" in tf:
            return int(tf.replace("m", "")) * 60
        elif "h" in tf:
            return int(tf.replace("h", "")) * 3600
        return 300
