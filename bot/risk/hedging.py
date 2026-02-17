"""
Hedging System - Portfolio protection for downside risk.

Hedging strategies:
1. Inverse ETF hedging (SH, SDS, SQQQ) during bearish regimes
2. Volatility hedging (UVXY, VXX) during high-vol periods
3. Position-level hedging (reduce exposure, tighten stops)
4. Correlation-based hedging (when portfolio is too long-biased)

This does NOT trade options for hedging (that's complex and risky).
Uses simple inverse ETFs that you can trade like any stock.
"""
from datetime import datetime

from bot.utils.logger import get_logger

log = get_logger("risk.hedging")

# Inverse ETF universe for hedging
HEDGE_INSTRUMENTS = {
    # S&P 500 hedges
    "SH": {"type": "inverse_1x", "tracks": "SPY", "description": "ProShares Short S&P500"},
    "SDS": {"type": "inverse_2x", "tracks": "SPY", "description": "ProShares UltraShort S&P500"},
    # Nasdaq hedges
    "PSQ": {"type": "inverse_1x", "tracks": "QQQ", "description": "ProShares Short QQQ"},
    "SQQQ": {"type": "inverse_3x", "tracks": "QQQ", "description": "ProShares UltraPro Short QQQ"},
    # Volatility
    "UVXY": {"type": "vol_long", "tracks": "VIX", "description": "ProShares Ultra VIX Short-Term"},
    "VXX": {"type": "vol_long", "tracks": "VIX", "description": "iPath VIX Short-Term Futures"},
}


class HedgingManager:
    """
    Manages portfolio hedging based on market regime and exposure.

    Simple approach:
    - Monitor net portfolio exposure (long vs short delta)
    - When regime is bearish or crisis, open inverse ETF positions
    - Scale hedge size based on regime confidence and portfolio size
    - Close hedges when regime improves
    """

    def __init__(self, config):
        self.config = config
        self.active_hedges = {}  # {symbol: hedge_info}
        self.hedge_history = []
        self.enabled = config.settings.get("hedging", {}).get("enabled", True)
        self.max_hedge_pct = config.settings.get("hedging", {}).get("max_hedge_pct", 0.30)
        self.auto_hedge = config.settings.get("hedging", {}).get("auto_hedge", True)

    def evaluate(self, positions, balance, regime_result):
        """
        Evaluate if hedging is needed and return hedge signals.

        Args:
            positions: Current position dict from engine
            balance: Current account balance
            regime_result: Output from RegimeDetector.detect()

        Returns:
            list of hedge signals to execute
        """
        if not self.enabled:
            return []

        signals = []
        hedge_rec = regime_result.get("hedge_recommendation", {})
        hedge_action = hedge_rec.get("action", "none")
        target_ratio = hedge_rec.get("hedge_ratio", 0)
        instruments = hedge_rec.get("instruments", [])

        # Calculate current portfolio exposure
        exposure = self._calculate_exposure(positions, balance)

        # Calculate current hedge coverage
        current_hedge = self._calculate_hedge_coverage(positions, balance)

        # Determine target hedge level
        target_hedge_value = balance * min(target_ratio, self.max_hedge_pct)

        if hedge_action == "none":
            # Close existing hedges if regime improved
            for symbol in list(self.active_hedges.keys()):
                if symbol in positions:
                    signals.append(self._create_close_hedge_signal(symbol, "Regime improved"))
            return signals

        if hedge_action in ("full_hedge", "partial_hedge", "vol_hedge"):
            # Check if we need more hedging
            hedge_gap = target_hedge_value - current_hedge

            if hedge_gap > balance * 0.02:  # Only hedge if gap > 2% of balance
                # Pick best instrument
                for instrument in instruments:
                    if instrument not in HEDGE_INSTRUMENTS:
                        continue
                    if instrument in positions:
                        continue  # Already have this hedge

                    signal = self._create_hedge_signal(
                        instrument, hedge_gap, balance, regime_result
                    )
                    if signal:
                        signals.append(signal)
                        break  # One hedge instrument at a time

        return signals

    def _calculate_exposure(self, positions, balance):
        """Calculate net portfolio long/short exposure."""
        long_value = 0
        short_value = 0
        hedge_value = 0

        for symbol, pos in positions.items():
            value = pos.get("entry_price", 0) * pos.get("quantity", 0)
            if symbol in HEDGE_INSTRUMENTS:
                hedge_value += value
            elif pos.get("direction") == "long":
                long_value += value
            else:
                short_value += value

        net_exposure = long_value - short_value
        gross_exposure = long_value + short_value

        return {
            "long_value": long_value,
            "short_value": short_value,
            "hedge_value": hedge_value,
            "net_exposure": net_exposure,
            "gross_exposure": gross_exposure,
            "net_pct": net_exposure / balance * 100 if balance > 0 else 0,
            "gross_pct": gross_exposure / balance * 100 if balance > 0 else 0,
        }

    def _calculate_hedge_coverage(self, positions, balance):
        """Calculate total hedge position value."""
        total = 0
        for symbol, pos in positions.items():
            if symbol in HEDGE_INSTRUMENTS:
                total += pos.get("entry_price", 0) * pos.get("quantity", 0)
        return total

    def _create_hedge_signal(self, instrument, target_value, balance, regime_result):
        """Create a buy signal for a hedge instrument."""
        info = HEDGE_INSTRUMENTS.get(instrument)
        if not info:
            return None

        # Cap hedge at max_hedge_pct
        max_value = balance * self.max_hedge_pct
        hedge_value = min(target_value, max_value)

        if hedge_value < 100:  # Min $100 hedge
            return None

        regime = regime_result.get("regime", "unknown")
        confidence = regime_result.get("confidence", 0.5)

        signal = {
            "symbol": instrument,
            "action": "buy",
            "confidence": confidence,
            "source": "hedging",
            "strategy": "hedge",
            "reason": f"Portfolio hedge ({regime}): {info['description']}",
            "asset_type": "stock",
            "stop_loss_pct": 0.10,  # Wide stop for hedges (10%)
            "hedge": True,  # Flag so risk manager treats differently
        }

        self.active_hedges[instrument] = {
            "instrument": instrument,
            "target_value": hedge_value,
            "regime": regime,
            "opened": datetime.now().isoformat(),
            "reason": regime_result.get("reason", ""),
        }

        log.info(
            f"HEDGE SIGNAL: BUY {instrument} (${hedge_value:.0f}) | "
            f"Regime: {regime} | {info['description']}"
        )

        return signal

    def _create_close_hedge_signal(self, symbol, reason):
        """Create a sell signal to close a hedge position."""
        if symbol in self.active_hedges:
            info = self.active_hedges.pop(symbol)
            self.hedge_history.append({
                **info,
                "closed": datetime.now().isoformat(),
                "close_reason": reason,
            })

        log.info(f"CLOSE HEDGE: SELL {symbol} | {reason}")

        return {
            "symbol": symbol,
            "action": "sell",
            "confidence": 0.8,
            "source": "hedging",
            "strategy": "hedge",
            "reason": f"Close hedge: {reason}",
        }

    def get_status(self):
        """Get hedging system status for dashboard."""
        return {
            "enabled": self.enabled,
            "auto_hedge": self.auto_hedge,
            "active_hedges": self.active_hedges,
            "max_hedge_pct": self.max_hedge_pct,
            "hedge_history": self.hedge_history[-20:],
            "instruments": {
                k: v["description"] for k, v in HEDGE_INSTRUMENTS.items()
            },
        }
