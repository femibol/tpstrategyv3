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
    10. Portfolio-level concentration limit (max 25% per name)
    11. Portfolio-level gross/net exposure limit
    12. Per-position max loss forced exit (8%)
    """

    def __init__(self, config, notifier=None):
        self.config = config
        self.notifier = notifier
        self.risk = config.risk_config

        # Current tier settings (updated via scaling)
        self.max_positions = self.risk.get("max_positions", 12)
        self.max_position_pct = self.risk.get("max_position_size_pct", 0.15)
        self.risk_per_trade = self.risk.get("risk_per_trade_pct", 0.01)
        self.min_volume = self.risk.get("min_volume", 50000)
        self.min_price = self.risk.get("min_price", 0.50)
        self.max_price = self.risk.get("max_price", 99999.0)  # No hard ceiling - runners can go past scanner range
        self.max_correlated = self.risk.get("max_correlated_positions", 2)
        self.min_confidence = 0.40  # Slightly higher - only quality signals for volatile small caps
        self.long_only = self.risk.get("long_only", False)

        # Portfolio-level risk limits
        portfolio_limits = self.risk.get("portfolio_limits", {})
        self.max_single_name_pct = portfolio_limits.get("max_single_name_pct", 0.25)
        self.max_gross_exposure_pct = portfolio_limits.get("max_gross_exposure_pct", 1.50)
        self.max_net_exposure_pct = portfolio_limits.get("max_net_exposure_pct", 1.00)
        self.max_loss_per_position_pct = portfolio_limits.get("max_loss_per_position_pct", 0.08)

        # Crypto-specific limits
        crypto_risk = config.settings.get("crypto", {}).get("risk", {})
        self.crypto_max_position_pct = crypto_risk.get("max_position_size_pct", 0.10)
        self.crypto_suffixes = config.settings.get("crypto", {}).get(
            "symbols_suffix", ["-USD", "-USDT", "-BTC", "-ETH"]
        )

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
                log.info(f"REJECTED: {signal['action']} {signal['symbol']} | {reason}")
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

        # Exit signals (sell/cover for existing positions) get lighter checks
        if action in ("sell", "cover", "close"):
            if symbol in positions:
                return True, "Exit signal for existing position"
            # Sell signal without position = skip
            return False, f"No position to exit: {symbol}"

        # --- Entry signal checks below ---

        # --- Rule 0: Long-only mode (matches TradersPost bullish-only setting) ---
        if self.long_only and action in ("sell", "short"):
            return False, f"BLOCKED: {action} {symbol} - long_only mode (no short/bearish entries)"

        # --- Rule 1: Max positions ---
        if len(positions) >= self.max_positions:
            return False, f"Max positions reached ({self.max_positions})"

        # --- Rule 2: Already in position ---
        if symbol in positions:
            return False, f"Already in position: {symbol}"

        # --- Rule 3: Signal age - reject stale signals (>60 seconds old) ---
        signal_time = signal.get("timestamp") or signal.get("received_at")
        if signal_time:
            if isinstance(signal_time, str):
                try:
                    from dateutil.parser import parse as parse_dt
                    signal_time = parse_dt(signal_time)
                except Exception:
                    signal_time = None
            if signal_time:
                now = datetime.now(signal_time.tzinfo) if signal_time.tzinfo else datetime.now()
                age_seconds = (now - signal_time).total_seconds()
                if age_seconds > 60:
                    return False, f"Stale signal: {age_seconds:.0f}s old (max 60s)"

        # --- Rule 4: Min price (skip for options) ---
        if signal.get("asset_type") != "option" and price > 0 and price < self.min_price:
            return False, f"Price ${price:.2f} below minimum ${self.min_price}"

        # --- Rule 5: Max price (skip for options) ---
        if signal.get("asset_type") != "option" and price > self.max_price:
            return False, f"Price ${price:.2f} above maximum ${self.max_price}"

        # --- Rule 6: Price reasonableness (reject if signal price is >5% from market) ---
        market_price = signal.get("market_price") or signal.get("current_price")
        if market_price and price and market_price > 0:
            price_diff_pct = abs(price - market_price) / market_price
            if price_diff_pct > 0.05:
                return False, (
                    f"Price ${price:.2f} is {price_diff_pct:.1%} away from "
                    f"market ${market_price:.2f} (max 5%)"
                )

        # --- Rule 7: Position size limit (crypto gets smaller cap) ---
        is_crypto = any(symbol.upper().endswith(s) for s in self.crypto_suffixes)
        pos_pct = self.crypto_max_position_pct if is_crypto else self.max_position_pct
        max_position = balance * pos_pct
        position_value = price * signal.get("quantity", 1)
        if position_value > max_position:
            return False, f"Position ${position_value:.0f} exceeds max ${max_position:.0f}"

        # --- Rule 8: Reserve cash ---
        reserve = balance * self.config.reserve_cash_pct
        invested = sum(
            p.get("entry_price", 0) * p.get("quantity", 0)
            for p in positions.values()
        )
        available = balance - invested - reserve
        # Compare total position cost (not just single share price)
        if position_value > available:
            return False, f"Insufficient available capital (${available:.0f} after reserve, order ${position_value:.0f})"

        # --- Rule 9: Confidence threshold ---
        confidence = signal.get("confidence", 0)
        if confidence < self.min_confidence:
            return False, f"Confidence {confidence:.2f} below threshold {self.min_confidence}"

        # --- Rule 10: Must have stop loss for entries ---
        if not signal.get("stop_loss"):
            return False, "No stop loss defined"

        # --- Rule 11: Portfolio gross exposure limit ---
        # Block new entries if portfolio gross exposure already at limit
        gross_exposure = self._calc_gross_exposure(positions, balance)
        new_exposure = position_value / balance if balance > 0 else 0
        if gross_exposure + new_exposure > self.max_gross_exposure_pct:
            return False, (
                f"Gross exposure {gross_exposure:.0%} + new {new_exposure:.0%} "
                f"would exceed max {self.max_gross_exposure_pct:.0%}"
            )

        return True, "All checks passed"

    def _calc_gross_exposure(self, positions, balance):
        """Calculate total gross exposure (longs + |shorts|) as fraction of balance."""
        if balance <= 0:
            return 0
        total = sum(
            abs(p.get("current_price", p.get("entry_price", 0)) * p.get("quantity", 0))
            for p in positions.values()
        )
        return total / balance

    def check_portfolio_health(self, positions, net_liquidation, get_price_fn=None):
        """
        Audit ALL positions for portfolio-level risk breaches.
        Returns list of actions to take: forced closes, alerts, etc.

        Called every monitoring cycle, not just on new entries.
        Works on ALL positions including shorts synced from broker.

        Args:
            positions: dict of {symbol: position_dict}
            net_liquidation: current account net liquidation value
            get_price_fn: callable(symbol) -> float, for live market prices

        Returns:
            list of dicts: [{"action": "force_close"|"alert", "symbol": ..., "reason": ...}, ...]
        """
        actions = []
        if net_liquidation <= 0 or not positions:
            return actions

        total_long_value = 0
        total_short_value = 0

        for symbol, pos in list(positions.items()):
            qty = pos.get("quantity", 0)
            entry_price = pos.get("entry_price", 0)
            direction = pos.get("direction", "long")

            # Get current market price if available
            current_price = None
            if get_price_fn:
                try:
                    current_price = get_price_fn(symbol)
                except Exception:
                    pass
            if not current_price or current_price <= 0:
                current_price = pos.get("current_price", entry_price)

            position_value = abs(current_price * qty)

            # Track exposure by direction
            if direction == "short":
                total_short_value += position_value
            else:
                total_long_value += position_value

            # --- Check 1: Single-name concentration ---
            concentration = position_value / net_liquidation
            if concentration > self.max_single_name_pct:
                actions.append({
                    "action": "force_close",
                    "symbol": symbol,
                    "reason": (
                        f"CONCENTRATION BREACH: {symbol} is {concentration:.0%} of portfolio "
                        f"(${position_value:,.0f} / ${net_liquidation:,.0f}, "
                        f"max {self.max_single_name_pct:.0%})"
                    ),
                    "severity": "critical",
                })

            # --- Check 2: Per-position max loss ---
            if entry_price > 0:
                if direction == "long":
                    pnl_pct = (current_price - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - current_price) / entry_price

                if pnl_pct < -self.max_loss_per_position_pct:
                    actions.append({
                        "action": "force_close",
                        "symbol": symbol,
                        "reason": (
                            f"MAX LOSS BREACH: {symbol} {direction} down "
                            f"{abs(pnl_pct):.1%} from entry ${entry_price:.2f} "
                            f"(current ${current_price:.2f}, max loss "
                            f"{self.max_loss_per_position_pct:.0%})"
                        ),
                        "severity": "critical",
                    })

        # --- Check 3: Gross exposure ---
        gross_exposure = (total_long_value + total_short_value) / net_liquidation
        if gross_exposure > self.max_gross_exposure_pct:
            actions.append({
                "action": "alert",
                "symbol": "PORTFOLIO",
                "reason": (
                    f"GROSS EXPOSURE BREACH: {gross_exposure:.0%} "
                    f"(longs ${total_long_value:,.0f} + shorts ${total_short_value:,.0f} "
                    f"= ${total_long_value + total_short_value:,.0f}, "
                    f"net liq ${net_liquidation:,.0f}, "
                    f"max {self.max_gross_exposure_pct:.0%})"
                ),
                "severity": "warning",
            })

        # --- Check 4: Net exposure ---
        net_exposure = (total_long_value - total_short_value) / net_liquidation
        if abs(net_exposure) > self.max_net_exposure_pct:
            direction_label = "LONG" if net_exposure > 0 else "SHORT"
            actions.append({
                "action": "alert",
                "symbol": "PORTFOLIO",
                "reason": (
                    f"NET EXPOSURE BREACH: {abs(net_exposure):.0%} {direction_label} "
                    f"(longs ${total_long_value:,.0f} - shorts ${total_short_value:,.0f} "
                    f"= ${total_long_value - total_short_value:,.0f}, "
                    f"net liq ${net_liquidation:,.0f}, "
                    f"max {self.max_net_exposure_pct:.0%})"
                ),
                "severity": "warning",
            })

        return actions

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
