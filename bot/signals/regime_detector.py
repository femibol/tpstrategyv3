"""
Market Regime Detector - Classifies current market conditions.

Regimes:
- BULL_TREND: Strong uptrend, momentum strategies work best
- BEAR_TREND: Strong downtrend, short/inverse strategies work
- SIDEWAYS: Range-bound, mean reversion shines
- HIGH_VOL: Elevated volatility, reduce position sizes
- LOW_VOL: Compressed volatility, breakout setups
- CRISIS: Extreme sell-off, full hedge mode
- GEOPOLITICAL: Sector rotation driven by geopolitical event (war, sanctions, etc.)
  Energy/defense up, travel/consumer down — sector momentum strategies dominate

Uses SPY/QQQ as market proxies to detect regime.
VIX (via UVXY proxy) for volatility regime shifts.
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
GEOPOLITICAL = "geopolitical"

# Strategy affinity per regime (which strategies work best in each regime)
# Includes all 14 active strategies
REGIME_STRATEGY_AFFINITY = {
    BULL_TREND: {
        "momentum": 1.3,
        "momentum_runner": 1.3,
        "rvol_momentum": 1.2,
        "rvol_scalp": 1.1,
        "prebreakout": 1.2,
        "premarket_gap": 1.2,
        "smc_forever": 1.2,
        "mean_reversion": 0.7,
        "vwap_scalp": 1.0,
        "pairs_trading": 0.8,
        "short_squeeze": 1.1,
        "pead": 1.1,
        "options_momentum": 1.0,
    },
    BEAR_TREND: {
        "momentum": 0.6,
        "momentum_runner": 0.6,
        "rvol_momentum": 0.7,
        "rvol_scalp": 0.8,
        "prebreakout": 0.5,
        "premarket_gap": 0.6,
        "smc_forever": 1.1,
        "mean_reversion": 0.8,
        "vwap_scalp": 1.0,
        "pairs_trading": 1.2,
        "short_squeeze": 0.5,
        "pead": 0.9,
        "options_momentum": 0.8,
    },
    SIDEWAYS: {
        "momentum": 0.5,
        "momentum_runner": 0.6,
        "rvol_momentum": 0.8,
        "rvol_scalp": 1.0,
        "prebreakout": 0.7,
        "premarket_gap": 0.8,
        "smc_forever": 0.8,
        "mean_reversion": 1.4,
        "vwap_scalp": 1.3,
        "pairs_trading": 1.3,
        "short_squeeze": 0.6,
        "pead": 1.0,
        "options_momentum": 1.0,
    },
    HIGH_VOL: {
        "momentum": 0.7,
        "momentum_runner": 0.8,
        "rvol_momentum": 1.0,
        "rvol_scalp": 0.7,
        "prebreakout": 0.6,
        "premarket_gap": 0.8,
        "smc_forever": 0.9,
        "mean_reversion": 1.2,
        "vwap_scalp": 0.6,
        "pairs_trading": 1.1,
        "short_squeeze": 0.8,
        "pead": 0.7,
        "options_momentum": 0.9,
    },
    LOW_VOL: {
        "momentum": 1.2,
        "momentum_runner": 1.0,
        "rvol_momentum": 0.9,
        "rvol_scalp": 0.7,
        "prebreakout": 1.2,
        "premarket_gap": 0.9,
        "smc_forever": 1.0,
        "mean_reversion": 0.8,
        "vwap_scalp": 0.7,
        "pairs_trading": 0.9,
        "short_squeeze": 0.7,
        "pead": 1.0,
        "options_momentum": 0.8,
    },
    CRISIS: {
        "momentum": 0.3,
        "momentum_runner": 0.3,
        "rvol_momentum": 0.4,
        "rvol_scalp": 0.3,
        "prebreakout": 0.2,
        "premarket_gap": 0.3,
        "smc_forever": 0.5,
        "mean_reversion": 0.4,
        "vwap_scalp": 0.3,
        "pairs_trading": 0.6,
        "short_squeeze": 0.3,
        "pead": 0.3,
        "options_momentum": 0.4,
    },
    GEOPOLITICAL: {
        # Geopolitical regime: sector rotation is king
        # Energy, defense, commodities surge; travel, consumer drop
        # Momentum strategies that follow sector themes dominate
        "momentum": 1.0,
        "momentum_runner": 1.5,     # Best for catching sector runners
        "rvol_momentum": 1.4,       # Volume-driven sector moves
        "rvol_scalp": 1.1,          # Quick scalps on volatile names
        "prebreakout": 1.0,
        "premarket_gap": 1.4,       # Gap plays amplified during geopolitical events
        "smc_forever": 0.8,
        "mean_reversion": 0.5,      # Don't buy dips in a crisis rotation
        "vwap_scalp": 0.7,
        "pairs_trading": 0.6,
        "short_squeeze": 0.7,
        "pead": 0.6,
        "options_momentum": 1.2,    # Options flow spikes on geopolitical events
    },
}


class RegimeDetector:
    """
    Detects current market regime using price action, volatility, trend indicators,
    VIX proxy tracking, and sector dispersion analysis.

    Uses SPY as primary market proxy.
    Uses UVXY/VXX as VIX proxy for volatility regime detection.
    Tracks sector dispersion to detect geopolitical rotation events.
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

        # VIX-based regime parameters
        self.vix_spike_threshold = 15.0   # VIX proxy single-day change % for regime shift
        self.vix_elevated_threshold = 8.0 # VIX proxy change % = elevated volatility

        # Sector dispersion for geopolitical detection
        self._sector_snapshot = {}  # {sector: avg_change_pct} — fed from scanner
        self._sector_dispersion = 0.0  # Std dev of sector returns
        self._hot_sectors = []   # Sectors surging (>3% avg)
        self._cold_sectors = []  # Sectors dumping (<-2% avg)

    def feed_sector_data(self, sector_changes):
        """Feed sector-level performance data for geopolitical regime detection.

        Args:
            sector_changes: dict of {sector: avg_change_pct} from scanner
                e.g., {"Energy": +5.2, "Aerospace": +4.1, "Consumer": -3.5}
        """
        if not sector_changes:
            return

        self._sector_snapshot = sector_changes
        changes = list(sector_changes.values())
        if len(changes) >= 2:
            self._sector_dispersion = float(np.std(changes))
            self._hot_sectors = [s for s, c in sector_changes.items() if c >= 3.0]
            self._cold_sectors = [s for s, c in sector_changes.items() if c <= -2.0]
        else:
            self._sector_dispersion = 0.0
            self._hot_sectors = []
            self._cold_sectors = []

    def detect(self, market_data, symbol="SPY"):
        """
        Detect current market regime from market data.

        Detection priority (highest to lowest):
        1. Crisis: SPY down 5%+ in 5 days
        2. Geopolitical: High sector dispersion + VIX spike + hot/cold sectors
        3. VIX spike: VIX proxy up 15%+ = immediate HIGH_VOL override
        4. Trend: Bull/Bear based on EMA crossover + ADX
        5. Volatility: High/Low vol based on ATR ratio
        6. Default: Sideways

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

        # --- VIX Proxy Detection ---
        # Check UVXY/VXX for sudden VIX spikes (geopolitical shocks, black swans)
        vix_change_pct = self._get_vix_proxy_change(market_data)

        # --- Crisis Detection ---
        if len(closes) >= 5:
            five_day_return = (closes[-1] - closes[-5]) / closes[-5]
            if five_day_return <= self.crisis_threshold:
                regime = CRISIS
                confidence = min(0.95, abs(five_day_return) * 10)
                reason = f"5-day return {five_day_return:.1%} (crisis threshold)"
                if vix_change_pct > 0:
                    reason += f" | VIX proxy +{vix_change_pct:.0f}%"
                result = self._make_result(regime, confidence, reason)
                self._update_history(result)
                return result

        # --- Geopolitical Regime Detection ---
        # Conditions: high sector dispersion + VIX elevated + clear winners/losers
        # e.g., Energy +5%, Defense +4%, Travel -5% = geopolitical rotation
        geo_detected = self._detect_geopolitical(vix_change_pct, vol_ratio)
        if geo_detected:
            result = geo_detected
            self._update_history(result)
            return result

        # --- VIX Spike Override ---
        # Single-day VIX spike of 15%+ = immediate HIGH_VOL, regardless of trend
        if vix_change_pct >= self.vix_spike_threshold:
            regime = HIGH_VOL
            confidence = min(0.95, 0.6 + vix_change_pct / 100)
            reason = f"VIX spike: proxy +{vix_change_pct:.0f}% | ATR {vol_ratio:.1f}x avg"
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
            if vix_change_pct >= self.vix_elevated_threshold:
                reason += f" | VIX proxy +{vix_change_pct:.0f}%"

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

    def _get_vix_proxy_change(self, market_data):
        """Get VIX proxy daily change % from UVXY or VXX.

        Returns the day's percentage change for the VIX proxy ETF.
        If neither is available, returns 0.
        """
        for proxy in ["UVXY", "VXX"]:
            data = market_data.get_data(proxy) if market_data else None
            if data is not None and len(data) >= 2:
                closes = data["close"].values
                if closes[-2] > 0:
                    change = (closes[-1] - closes[-2]) / closes[-2] * 100
                    return round(change, 1)
        return 0.0

    def _detect_geopolitical(self, vix_change_pct, vol_ratio):
        """Detect geopolitical regime: sector rotation driven by macro events.

        Geopolitical regime = markets aren't crashing uniformly; instead,
        specific sectors surge while others dump. This is different from
        CRISIS (everything down) or HIGH_VOL (general uncertainty).

        Signals:
        - High sector dispersion (std dev of sector returns > 3%)
        - At least 1 hot sector (avg +3%) AND 1 cold sector (avg -2%)
        - VIX elevated (proxy up 8%+) OR vol ratio > 1.3x

        Returns regime result dict if geopolitical detected, else None.
        """
        if self._sector_dispersion < 3.0:
            return None  # Sectors moving together, not rotation

        if not self._hot_sectors or not self._cold_sectors:
            return None  # Need clear winners AND losers

        # Need some vol confirmation (VIX spike or elevated ATR)
        vol_confirmed = vix_change_pct >= self.vix_elevated_threshold or vol_ratio > 1.3

        if not vol_confirmed:
            return None

        confidence = min(0.90, 0.5 + self._sector_dispersion * 0.05 +
                         len(self._hot_sectors) * 0.05 +
                         len(self._cold_sectors) * 0.05)

        hot_str = ", ".join(self._hot_sectors[:3])
        cold_str = ", ".join(self._cold_sectors[:3])
        reason = (
            f"Geopolitical rotation: dispersion {self._sector_dispersion:.1f}% | "
            f"Hot: [{hot_str}] | Cold: [{cold_str}]"
        )
        if vix_change_pct > 0:
            reason += f" | VIX proxy +{vix_change_pct:.0f}%"

        log.info(f"GEOPOLITICAL REGIME DETECTED: {reason}")
        return self._make_result(GEOPOLITICAL, confidence, reason)

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
            GEOPOLITICAL: 0.7,  # Reduced overall, but sector-specific plays get boosted
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
        elif regime == GEOPOLITICAL:
            return {
                "action": "sector_rotation",
                "instruments": [],  # Don't hedge — rotate into hot sectors
                "hedge_ratio": 0,
                "hot_sectors": self._hot_sectors,
                "cold_sectors": self._cold_sectors,
                "reason": (
                    f"Geopolitical rotation — favor {', '.join(self._hot_sectors[:2])} "
                    f"| avoid {', '.join(self._cold_sectors[:2])}"
                ),
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
            "hedge_recommendation": self._get_hedge_recommendation(
                self.current_regime, self.regime_confidence
            ),
            "sector_snapshot": self._sector_snapshot,
            "sector_dispersion": self._sector_dispersion,
            "hot_sectors": self._hot_sectors,
            "cold_sectors": self._cold_sectors,
            "history": self.regime_history[-20:],
        }
