"""
Momentum / Trend Following Strategy
- Ride strong trends in liquid stocks
- EMA crossover with ADX confirmation
- Volume surge validation
- ATR-based stops and targets (like hedge funds)
"""
import numpy as np
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.momentum")


class MomentumStrategy(BaseStrategy):
    """
    Momentum Strategy - let winners run, cut losers fast.

    Logic:
    1. Fast EMA crosses above Slow EMA = bullish
    2. ADX > 25 confirms strong trend
    3. Volume must be 1.5x+ average
    4. Enter on pullback to fast EMA in trend
    5. Stop loss at 2x ATR, target at 4x ATR (2:1 R/R minimum)
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.fast_ema = config.get("fast_ema", 8)
        self.slow_ema = config.get("slow_ema", 21)
        self.signal_ema = config.get("signal_ema", 5)
        self.adx_threshold = config.get("adx_threshold", 25)
        self.vol_surge = config.get("volume_surge_multiplier", 1.5)
        self.atr_period = config.get("atr_period", 14)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 2.0)
        self.atr_target_mult = config.get("atr_target_multiplier", 4.0)
        self.max_hold = config.get("max_holding_bars", 40)
        self.breakout_lookback = config.get("breakout_lookback", 20)

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
        """Analyze a single symbol for momentum entry."""
        bars = market_data.get_bars(symbol, 60)
        if bars is None or len(bars) < 40:
            self.scan_results[symbol] = {"status": "no_data", "verdict": "WAIT"}
            return None

        closes = bars["close"].values
        highs = bars["high"].values
        lows = bars["low"].values
        volumes = bars["volume"].values
        current_price = closes[-1]

        # EMAs
        fast_ema = self.indicators.ema(closes, self.fast_ema)
        slow_ema = self.indicators.ema(closes, self.slow_ema)

        if fast_ema is None or slow_ema is None:
            return None

        # EMA crossover state
        ema_bullish = fast_ema[-1] > slow_ema[-1]
        ema_just_crossed = (
            fast_ema[-1] > slow_ema[-1] and fast_ema[-2] <= slow_ema[-2]
        )

        # ADX - trend strength
        adx = self.indicators.adx(highs, lows, closes, period=14)
        strong_trend = adx is not None and adx > self.adx_threshold

        # Volume surge
        avg_vol = np.mean(volumes[-20:])
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        vol_confirmed = vol_ratio >= self.vol_surge

        # ATR for stop/target calculation
        atr = self.indicators.atr(highs, lows, closes, period=self.atr_period)
        if atr is None or atr <= 0:
            self.scan_results[symbol] = {"status": "low_volatility", "verdict": "WAIT"}
            return None

        # Breakout detection - price above N-bar high
        lookback_high = np.max(highs[-self.breakout_lookback:-1])
        breakout = current_price > lookback_high

        # Trend direction
        if ema_just_crossed:
            trend = "CROSS UP"
        elif ema_bullish:
            trend = "BULLISH"
        else:
            trend = "BEARISH"

        checks = {
            "ema_bullish": ema_bullish,
            "ema_cross": ema_just_crossed,
            "strong_trend": strong_trend,
            "vol_confirmed": vol_confirmed,
            "breakout": breakout,
        }
        passed = sum(1 for v in checks.values() if v)
        if ema_bullish and (strong_trend or breakout) and vol_confirmed:
            verdict = "BUY SIGNAL"
        elif passed >= 3:
            verdict = "WARMING UP"
        else:
            verdict = "NEUTRAL"

        self.scan_results[symbol] = {
            "price": round(current_price, 2),
            "fast_ema": round(fast_ema[-1], 2),
            "slow_ema": round(slow_ema[-1], 2),
            "ema_spread": round((fast_ema[-1] - slow_ema[-1]) / slow_ema[-1] * 100, 3),
            "trend": trend,
            "adx": round(adx, 1) if adx else 0,
            "atr": round(atr, 2),
            "atr_pct": round(atr / current_price * 100, 2),
            "vol_ratio": round(vol_ratio, 1),
            "breakout_level": round(lookback_high, 2),
            "breakout": breakout,
            "checks": checks,
            "checks_passed": passed,
            "verdict": verdict,
        }

        # --- BUY Signal ---
        # Need: EMA bullish + (strong trend OR breakout) + volume
        if ema_bullish and (strong_trend or breakout) and vol_confirmed:
            confidence = 0.5

            # Bonus for fresh crossover
            if ema_just_crossed:
                confidence += 0.15

            # Bonus for strong ADX
            if adx and adx > 30:
                confidence += 0.1

            # Bonus for breakout
            if breakout:
                confidence += 0.15

            # Volume strength bonus
            if vol_ratio > 2.0:
                confidence += 0.1

            confidence = min(1.0, confidence)

            # ATR-based stops (this is what the big funds do)
            stop_loss = current_price - (self.atr_stop_mult * atr)
            take_profit = current_price + (self.atr_target_mult * atr)

            reasons = []
            if ema_just_crossed:
                reasons.append("EMA cross")
            elif ema_bullish:
                reasons.append("EMA trend")
            if strong_trend:
                reasons.append(f"ADX={adx:.0f}")
            if breakout:
                reasons.append(f"breakout>{lookback_high:.2f}")
            reasons.append(f"Vol={vol_ratio:.1f}x")

            signal = {
                "symbol": symbol,
                "action": "buy",
                "price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence": confidence,
                "reason": f"Momentum BUY: {', '.join(reasons)}",
                "max_hold_bars": self.max_hold,
                "bar_seconds": self._timeframe_to_seconds(),
                "trailing_stop_pct": atr / current_price,  # ATR-based trail
            }

            log.info(f"SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f}")
            self.signals_generated += 1
            return signal

        return None

    def _timeframe_to_seconds(self):
        tf = self.timeframe
        if "m" in tf:
            return int(tf.replace("m", "")) * 60
        elif "h" in tf:
            return int(tf.replace("h", "")) * 3600
        return 900  # 15min default
