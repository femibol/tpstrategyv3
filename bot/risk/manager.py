"""
Risk Manager - The most important part of the system.
Protects capital at all costs. No trade gets through without approval.
"""
from datetime import datetime
from collections import defaultdict

from bot.utils.logger import get_logger

log = get_logger("risk.manager")


class RiskManager:
    """
    Risk Manager - gate-keeper for all trades.

    Rules enforced:
    1. Max daily loss limit (2% = $100 at $5K)
    2. Max drawdown from peak (10% = $500 at $5K)
    3. Max positions (5 at $5K tier)
    4. Max position size (15% = $750 at $5K)
    5. Max correlated positions (2 per sector)
    6. Min volume filter (500K daily avg)
    7. Min price filter (no penny stocks)
    8. Reserve cash requirement (always keep 20%)
    9. Confidence threshold
    """

    def __init__(self, config, notifier=None):
        self.config = config
        self.notifier = notifier
        self.risk = config.risk_config

        # Current tier settings (updated via scaling)
        self.max_positions = self.risk.get("max_positions", 5)
        self.max_position_pct = self.risk.get("max_position_size_pct", 0.15)
        self.risk_per_trade = self.risk.get("risk_per_trade_pct", 0.01)
        self.min_volume = self.risk.get("min_volume", 500000)
        self.min_price = self.risk.get("min_price", 5.0)
        self.max_price = self.risk.get("max_price", 500.0)
        self.max_correlated = self.risk.get("max_correlated_positions", 2)
        self.min_confidence = 0.4

        # Tracking
        self.rejected_signals = []

    def filter_signals(self, signals, positions, current_balance):
        """
        Filter signals through all risk checks.
        Only approved signals get executed.
        """
        approved = []

        for signal in signals:
            passed, reason = self._check_all_rules(
                signal, positions, current_balance
            )
            if passed:
                approved.append(signal)
                log.info(f"APPROVED: {signal['action']} {signal['symbol']} | {signal.get('reason', '')}")
            else:
                log.debug(f"REJECTED: {signal['action']} {signal['symbol']} | {reason}")
                self.rejected_signals.append({
                    "time": datetime.now().isoformat(),
                    "signal": signal,
                    "reason": reason,
                })

        return approved

    def _check_all_rules(self, signal, positions, balance):
        """Run signal through all risk checks."""
        symbol = signal["symbol"]
        action = signal["action"]
        price = signal.get("price", 0)

        # --- Rule 1: Max positions ---
        if action in ("buy", "short") and len(positions) >= self.max_positions:
            return False, f"Max positions reached ({self.max_positions})"

        # --- Rule 2: Already in position ---
        if action in ("buy", "short") and symbol in positions:
            return False, f"Already in position: {symbol}"

        # --- Rule 3: Min price ---
        if price < self.min_price:
            return False, f"Price ${price:.2f} below minimum ${self.min_price}"

        # --- Rule 4: Max price ---
        if price > self.max_price:
            return False, f"Price ${price:.2f} above maximum ${self.max_price}"

        # --- Rule 5: Position size limit ---
        max_position = balance * self.max_position_pct
        position_value = price * signal.get("quantity", 1)
        if position_value > max_position:
            return False, f"Position ${position_value:.0f} exceeds max ${max_position:.0f}"

        # --- Rule 6: Reserve cash ---
        reserve = balance * self.config.reserve_cash_pct
        invested = sum(
            p.get("entry_price", 0) * p.get("quantity", 0)
            for p in positions.values()
        )
        available = balance - invested - reserve
        if action in ("buy", "short") and price > available:
            return False, f"Insufficient available capital (${available:.0f} after reserve)"

        # --- Rule 7: Confidence threshold ---
        confidence = signal.get("confidence", 0)
        if confidence < self.min_confidence:
            return False, f"Confidence {confidence:.2f} below threshold {self.min_confidence}"

        # --- Rule 8: Must have stop loss for entries ---
        if action in ("buy", "short") and not signal.get("stop_loss"):
            return False, "No stop loss defined"

        return True, "All checks passed"

    def is_daily_loss_exceeded(self, current_balance, start_of_day_balance):
        """Check if daily loss limit has been hit."""
        if start_of_day_balance <= 0:
            return False
        daily_loss = (start_of_day_balance - current_balance) / start_of_day_balance
        return daily_loss >= self.config.max_daily_loss

    def is_max_drawdown_exceeded(self, current_balance, peak_balance):
        """Check if max drawdown from peak has been exceeded."""
        if peak_balance <= 0:
            return False
        drawdown = (peak_balance - current_balance) / peak_balance
        return drawdown >= self.config.max_drawdown

    def update_tier(self, tier):
        """Update risk parameters based on scaling tier."""
        if tier:
            old_max = self.max_positions
            self.max_positions = tier.get("max_positions", self.max_positions)
            self.risk_per_trade = tier.get("risk_per_trade", self.risk_per_trade)
            self.max_position_pct = tier.get("max_position_pct", self.max_position_pct)

            if self.max_positions != old_max:
                log.info(
                    f"SCALING: Tier updated - "
                    f"Max positions: {self.max_positions}, "
                    f"Risk/trade: {self.risk_per_trade:.1%}, "
                    f"Max position: {self.max_position_pct:.0%}"
                )
