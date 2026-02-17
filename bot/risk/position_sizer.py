"""
Position Sizer - Determines how many shares to buy.
Uses fixed-risk method: risk a fixed % of capital per trade.
"""
import math
from bot.utils.logger import get_logger

log = get_logger("risk.position_sizer")


class PositionSizer:
    """
    Position sizing using fixed-risk model.

    The key formula (used by professional traders):
    Position Size = (Account Risk $) / (Per-Share Risk $)

    Where:
    - Account Risk $ = Balance * Risk Per Trade % (e.g., $5000 * 1% = $50)
    - Per-Share Risk $ = Entry Price - Stop Loss Price

    This means:
    - If AAPL is $150 with stop at $147 (risk $3/share):
      Shares = $50 / $3 = 16 shares ($2,400 position)
    - If AMD is $100 with stop at $97 (risk $3/share):
      Shares = $50 / $3 = 16 shares ($1,600 position)

    The position size automatically adjusts based on volatility.
    Volatile stocks = smaller positions. Stable stocks = larger positions.
    """

    def __init__(self, config):
        self.config = config
        self.risk_per_trade_pct = config.risk_per_trade
        self.max_position_pct = config.risk_config.get("max_position_size_pct", 0.15)
        self.reserve_pct = config.reserve_cash_pct

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

        # Minimum 1 share
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
                f"({risk_amount / balance:.1%} of account)"
            )

        return shares
