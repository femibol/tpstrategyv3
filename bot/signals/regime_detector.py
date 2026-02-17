"""
Market Regime Detector - Classifies current market conditions.

Regimes:
- BULL_TREND: Strong uptrend, momentum strategies work best
- BEAR_TREND: Strong downtrend, short/inverse strategies work
- SIDEWAYS: Range-bound, mean reversion shines
- HIGH_VOL: Elevated volatility, reduce position sizes
- LOW_VOL: Compressed volatility, breakout setups
- CRISIS: Extreme sell-off, full hedge mode

Uses SPY/QQQ as market proxies to detect regime.
"""
import numpy as np
from datetime import datetime

from bot.utils.logger import get_logger

log = get_logger("signals.regime_detector")

# Regime constants
BULL_TREND = "bull_trend"
BEAR_TREND = "bear_trend"
SIDEWAYS = "sideways"
HIGH_VOL = "high_vol"
LOW_VOL = "low_vol"
CRISIS = "crisis"

# Strategy affinity per regime (which strategies work best in each regime)
REGIME_STRATEGY_AFFINITY = {
    BULL_TREND: {
        "momentum": 1.3,
        "smc_forever": 1.2,
        "mean_reversion": 0.7,
        "vwap_scalp": 1.0,
        "pairs_trading": 0.8,
    },
    BEAR_TREND: {
        "momentum": 0.6,
        "smc_forever": 1.1,
        "mean_reversion": 0.8,
        "vwap_scalp": 1.0,
        "pairs_trading": 1.2,
    },
    SIDEWAYS: {
        "momentum": 0.5,
        "smc_forever": 0.8,
        "mean_reversion": 1.4,
        "vwap_scalp": 1.3,
        "pairs_trading": 1.3,
    },
    HIGH_VOL: {
        "momentum": 0.7,
        "smc_forever": 0.9,
        "mean_reversion": 1.2,
        "vwap_scalp": 0.6,
        "pairs_trading": 1.1,
    },
    LOW_VOL: {
        "momentum": 1.2,
        "smc_forever": 1.0,
        "mean_reversion": 0.8,
        "vwap_scalp": 0.7,
        "pairs_trading": 0.9,
    },
    CRISIS: {
        "momentum": 0.3,
        "smc_forever": 0.5,
        "mean_reversion": 0.4,
        "vwap_scalp": 0.3,
        "pairs_trading": 0.6,
    },
}


class RegimeDetector:
    """
    Detects current market regime using price action, volatility, and trend indicators.

    Uses SPY as primary market proxy.
    """

    def __init__(self, indicators):
        self.indicators = indicators
        self.current_regime = SIDEWAYS
        self.regime_history = []
        self.regime_confidence = 0.0

        # Detection parameters
        self.trend_ema_fast = 20
        self.trend_ema_slow = 50
        self.vol_period = 20
        self.vol_high_threshold = 1.5   # VIX equivalent: above 1.5x avg = high vol
        self.vol_low_threshold = 0.6    # Below 0.6x avg = compressed
        self.crisis_threshold = -0.05   # 5% drop in 5 days = crisis
        self.adx_trend_threshold = 25   # ADX > 25 = trending

    def detect(self, market_data, symbol="SPY"):
        """
        Detect current market regime from market data.

        Args:
            market_data: MarketDataFeed instance
            symbol: Market proxy symbol (default SPY)

        Returns:
            dict with regime, confidence, and strategy multipliers
        """
        data = market_data.get_data(symbol) if market_data else None
        if data is None or len(data) < self.trend_ema_slow + 10:
            return self._make_result(SIDEWAYS, 0.3, "Insufficient data")

        closes = np.array(data["close"].values, dtype=float)
        highs = np.array(data["high"].values, dtype=float)
        lows = np.array(data["low"].values, dtype=float)

        # Calculate indicators
        fast_ema = self.indicators.ema(closes, self.trend_ema_fast)
        slow_ema = self.indicators.ema(closes, self.trend_ema_slow)
        rsi = self.indicators.rsi(closes)
        adx = self.indicators.adx(highs, lows, closes)
        atr = self.indicators.atr(highs, lows, closes)

        if fast_ema is None or slow_ema is None:
            return self._make_result(SIDEWAYS, 0.3, "Indicator calculation failed")

        current_price = closes[-1]
        fast_val = fast_ema[-1]
        slow_val = slow_ema[-1]

        # --- Volatility Analysis ---
        if atr and current_price > 0:
            atr_pct = atr / current_price
            # Historical ATR for comparison
            atr_history = []
            for i in range(self.vol_period, len(closes)):
                h_atr = self.indicators.atr(
                    highs[:i+1], lows[:i+1], closes[:i+1]
                )
                if h_atr:
                    atr_history.append(h_atr)

            if atr_history:
                avg_atr = np.mean(atr_history[-self.vol_period:])
                vol_ratio = atr / avg_atr if avg_atr > 0 else 1.0
            else:
                vol_ratio = 1.0
        else:
            vol_ratio = 1.0
            atr_pct = 0

        # --- Crisis Detection ---
        if len(closes) >= 5:
            five_day_return = (closes[-1] - closes[-5]) / closes[-5]
            if five_day_return <= self.crisis_threshold:
                regime = CRISIS
                confidence = min(0.95, abs(five_day_return) * 10)
                reason = f"5-day return {five_day_return:.1%} (crisis threshold)"
                result = self._make_result(regime, confidence, reason)
                self._update_history(result)
                return result

        # --- Trend Detection ---
        trend_bullish = fast_val > slow_val
        trend_strength = abs(fast_val - slow_val) / slow_val * 100 if slow_val > 0 else 0
        strong_trend = adx is not None and adx > self.adx_trend_threshold

        # --- Regime Classification ---
        if strong_trend and trend_bullish and trend_strength > 0.5:
            regime = BULL_TREND
            confidence = min(0.9, 0.5 + trend_strength * 0.1)
            reason = f"Strong uptrend: EMA spread {trend_strength:.1f}%, ADX {adx:.0f}"

        elif strong_trend and not trend_bullish and trend_strength > 0.5:
            regime = BEAR_TREND
            confidence = min(0.9, 0.5 + trend_strength * 0.1)
            reason = f"Strong downtrend: EMA spread -{trend_strength:.1f}%, ADX {adx:.0f}"

        elif vol_ratio > self.vol_high_threshold:
            regime = HIGH_VOL
            confidence = min(0.85, 0.4 + (vol_ratio - 1) * 0.3)
            reason = f"High volatility: ATR {vol_ratio:.1f}x average"

        elif vol_ratio < self.vol_low_threshold:
            regime = LOW_VOL
            confidence = min(0.8, 0.4 + (1 - vol_ratio) * 0.5)
            reason = f"Low volatility: ATR {vol_ratio:.1f}x average (compressed)"

        else:
            regime = SIDEWAYS
            confidence = 0.5
            reason = f"Range-bound: EMA spread {trend_strength:.1f}%, vol ratio {vol_ratio:.1f}x"

        result = self._make_result(regime, confidence, reason)
        self._update_history(result)
        return result

    def _make_result(self, regime, confidence, reason):
        """Create a regime detection result."""
        self.current_regime = regime
        self.regime_confidence = confidence

        return {
            "regime": regime,
            "confidence": confidence,
            "reason": reason,
            "strategy_multipliers": REGIME_STRATEGY_AFFINITY.get(regime, {}),
            "risk_multiplier": self._get_risk_multiplier(regime),
            "hedge_recommendation": self._get_hedge_recommendation(regime, confidence),
            "timestamp": datetime.now().isoformat(),
        }

    def _get_risk_multiplier(self, regime):
        """Get risk adjustment multiplier for the regime."""
        multipliers = {
            BULL_TREND: 1.0,    # Normal risk
            BEAR_TREND: 0.7,    # Reduce risk
            SIDEWAYS: 0.9,      # Slightly reduced
            HIGH_VOL: 0.5,      # Half risk in volatile markets
            LOW_VOL: 0.8,       # Slightly reduced (breakout risk)
            CRISIS: 0.2,        # Minimal risk, mostly cash
        }
        return multipliers.get(regime, 0.8)

    def _get_hedge_recommendation(self, regime, confidence):
        """Get hedging recommendation based on regime."""
        if regime == CRISIS:
            return {
                "action": "full_hedge",
                "instruments": ["SH", "SQQQ", "UVXY"],
                "hedge_ratio": 0.5,
                "reason": "Crisis mode - heavy downside protection",
            }
        elif regime == BEAR_TREND and confidence > 0.6:
            return {
                "action": "partial_hedge",
                "instruments": ["SH", "SDS"],
                "hedge_ratio": 0.25,
                "reason": "Bear trend - moderate downside protection",
            }
        elif regime == HIGH_VOL:
            return {
                "action": "vol_hedge",
                "instruments": ["UVXY", "VXX"],
                "hedge_ratio": 0.10,
                "reason": "High volatility - small vol hedge",
            }
        else:
            return {
                "action": "none",
                "instruments": [],
                "hedge_ratio": 0,
                "reason": "No hedging needed in current regime",
            }

    def _update_history(self, result):
        """Track regime changes."""
        self.regime_history.append({
            "regime": result["regime"],
            "confidence": result["confidence"],
            "timestamp": result["timestamp"],
        })
        # Keep last 100
        if len(self.regime_history) > 100:
            self.regime_history = self.regime_history[-100:]

        # Log regime changes
        if len(self.regime_history) >= 2:
            prev = self.regime_history[-2]["regime"]
            curr = result["regime"]
            if prev != curr:
                log.info(
                    f"REGIME CHANGE: {prev.upper()} -> {curr.upper()} "
                    f"(confidence: {result['confidence']:.0%})"
                )

    def get_status(self):
        """Get regime detector status for dashboard."""
        return {
            "current_regime": self.current_regime,
            "confidence": self.regime_confidence,
            "strategy_multipliers": REGIME_STRATEGY_AFFINITY.get(self.current_regime, {}),
            "risk_multiplier": self._get_risk_multiplier(self.current_regime),
            "history": self.regime_history[-20:],
        }
