"""
Position Sizer - Determines how many shares to buy.
Uses fixed-risk method: risk a fixed % of capital per trade.
Auto-adjusts quantity based on stock price tier.
"""
import math
from bot.utils.logger import get_logger

log = get_logger("risk.position_sizer")

# Price tiers for auto-adjusting quantity limits
# Expensive stocks get fewer shares, cheap stocks get more
PRICE_TIERS = [
    # (max_price, min_shares, max_shares_cap)
    (10,     5,   500),   # Penny/low-price: 5-500 shares
    (50,     3,   200),   # Mid-low: 3-200 shares
    (150,    2,   100),   # Mid: 2-100 shares
    (500,    1,    50),   # High: 1-50 shares
    (2000,   1,    20),   # Premium: 1-20 shares (NVDA, MSFT)
    (99999,  1,    10),   # Ultra: 1-10 shares (BRK.A etc)
]


class PositionSizer:
    """
    Position sizing using fixed-risk model with price-tier awareness.

    The key formula (used by professional traders):
    Position Size = (Account Risk $) / (Per-Share Risk $)

    Price-tier guards ensure:
    - Expensive stocks ($180+ NVDA): min 1, max 20 shares
    - Mid-range stocks ($50-150 AAPL): min 2, max 100 shares
    - Cheap stocks (<$10): min 5, max 500 shares

    The position size automatically adjusts based on volatility.
    Volatile stocks = smaller positions. Stable stocks = larger positions.
    """

    def __init__(self, config):
        self.config = config
        self.risk_per_trade_pct = config.risk_per_trade
        self.max_position_pct = config.risk_config.get("max_position_size_pct", 0.15)
        self.reserve_pct = config.reserve_cash_pct

    def _get_tier_limits(self, price):
        """Get min/max share limits based on stock price tier."""
        for max_price, min_shares, max_shares in PRICE_TIERS:
            if price <= max_price:
                return min_shares, max_shares
        return 1, 10  # fallback

    def calculate(self, balance, price, stop_loss, strategy_allocation=1.0):
        """
        Calculate position size in shares (or contracts for options).

        Args:
            balance: Current account balance
            price: Entry price
            stop_loss: Stop loss price
            strategy_allocation: Fraction of capital for this strategy (0-1)

        Returns:
            int: Number of shares/contracts (0 if trade doesn't meet criteria)
        """
        if price <= 0 or stop_loss <= 0:
            return 0

        # Available capital (after reserve)
        available = balance * (1 - self.reserve_pct) * strategy_allocation

        # Max position value scales with account size (no hard dollar cap)
        max_position = min(
            balance * self.max_position_pct,
            available
        )

        # Risk per trade in dollars
        risk_dollars = balance * self.risk_per_trade_pct

        # Per-share risk
        per_share_risk = abs(price - stop_loss)
        if per_share_risk <= 0:
            return 0

        # Calculate shares based on risk
        shares_by_risk = math.floor(risk_dollars / per_share_risk)

        # Cap by max position size
        shares_by_max = math.floor(max_position / price)

        # Take the smaller of the two
        shares = min(shares_by_risk, shares_by_max)

        # Apply price-tier limits (auto-adjust for expensive vs cheap stocks)
        tier_min, tier_max = self._get_tier_limits(price)
        shares = min(shares, tier_max)

        # Ensure minimum shares if we can afford it
        if shares < tier_min and tier_min * price <= available:
            shares = tier_min

        # Floor at 0 (never negative)
        shares = max(0, shares)

        # Final sanity check - position value shouldn't exceed available capital
        if shares * price > available:
            shares = math.floor(available / price)

        if shares > 0:
            position_value = shares * price
            risk_amount = shares * per_share_risk
            log.info(
                f"Position size: {shares} shares @ ${price:.2f} = "
                f"${position_value:,.2f} | Risk: ${risk_amount:.2f} "
                f"({risk_amount / balance:.1%} of account) "
                f"[tier: {tier_min}-{tier_max} shares]"
            )
        else:
            log.warning(
                f"Position size 0 for ${price:.2f} stock - "
                f"available: ${available:.2f}, risk: ${risk_dollars:.2f}"
            )

        return shares
