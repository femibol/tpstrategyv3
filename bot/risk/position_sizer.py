"""
Position Sizer - Determines how many shares to buy.
Uses fixed-risk method: risk a fixed % of capital per trade.
Auto-adjusts quantity based on stock price tier.
"""
import math
from bot.utils.logger import get_logger

log = get_logger("risk.position_sizer")

# Price tiers for auto-adjusting quantity limits
# Optimized for $0.50-$50 runner strategy
# More shares on cheap stocks = bigger dollar profit on % moves
PRICE_TIERS = [
    # (max_price, min_shares, max_shares_cap)
    # Caps raised — old caps were capping positions well below max_position_size_pct
    # A $47K account with 15% max position = $7K. Tier caps must allow that.
    (2,      10,  5000),  # Sub-$2 runners: 10-5000 shares ($10K max position value)
    (5,      10,  2000),  # $2-$5 range: 10-2000 shares ($10K max)
    (10,     5,   1000),  # $5-$10 range: 5-1000 shares ($10K max)
    (25,     3,   500),   # $10-$25 range: 3-500 shares ($12.5K max)
    (50,     2,   300),   # $25-$50 range: 2-300 shares ($15K max)
    (150,    1,   150),   # $50-$150 (runners past scanner): 1-150 shares
    (500,    1,    50),   # High: 1-50 shares
    (99999,  1,    10),   # Ultra: 1-10 shares
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

        # Crypto-specific limits
        crypto_risk = config.settings.get("crypto", {}).get("risk", {})
        self.crypto_max_position_pct = crypto_risk.get("max_position_size_pct", 0.10)
        self.crypto_suffixes = config.settings.get("crypto", {}).get(
            "symbols_suffix", ["-USD", "-USDT", "-BTC", "-ETH"]
        )

    def update_tier(self, tier):
        """Update sizing parameters from scaling tier."""
        if tier:
            self.risk_per_trade_pct = tier.get("risk_per_trade", self.risk_per_trade_pct)
            self.max_position_pct = tier.get("max_position_pct", self.max_position_pct)

    def _get_tier_limits(self, price):
        """Get min/max share limits based on stock price tier."""
        for max_price, min_shares, max_shares in PRICE_TIERS:
            if price <= max_price:
                return min_shares, max_shares
        return 1, 10  # fallback

    def _is_crypto(self, symbol):
        """Check if symbol is a crypto ticker."""
        if not symbol:
            return False
        return any(symbol.upper().endswith(s) for s in self.crypto_suffixes)

    def _kelly_adjustment(self, trade_history, base_risk_pct):
        """Calculate risk multiplier using Risk-Constrained Kelly Criterion.

        Kelly = (W*B - L) / B
          where W = win rate, L = loss rate, B = avg_win/avg_loss ratio

        We use HALF-KELLY (k/2) for safety — full Kelly is too volatile.
        Bounded to [0.25x, 2.0x] of base risk to prevent extreme sizing.

        Returns: multiplier (float) to apply to base risk_per_trade_pct.
        """
        if not trade_history or len(trade_history) < 20:
            # Need at least 20 trades for statistical significance
            return 1.0

        recent = list(trade_history)[-100:]  # Last 100 trades for rolling Kelly
        wins = [t for t in recent if t.get("pnl", 0) > 0]
        losses = [t for t in recent if t.get("pnl", 0) < 0]

        if not wins or not losses:
            return 1.0  # Insufficient data for Kelly

        win_rate = len(wins) / len(recent)
        avg_win = sum(t.get("pnl", 0) for t in wins) / len(wins)
        avg_loss = abs(sum(t.get("pnl", 0) for t in losses) / len(losses))

        if avg_loss == 0:
            return 1.0

        # Kelly formula (b = avg_win / avg_loss)
        b = avg_win / avg_loss
        kelly_pct = (win_rate * b - (1 - win_rate)) / b

        # Half-Kelly for safety (pros rarely use full Kelly — too volatile)
        safe_kelly = kelly_pct * 0.5

        # Convert to multiplier against base risk
        # If Kelly says 2% optimal and base is 1%, multiplier = 2x
        if safe_kelly <= 0:
            # Negative edge — scale way down, we're losing
            return 0.25

        multiplier = safe_kelly / base_risk_pct

        # Bound: never above 2x, never below 0.25x
        return max(0.25, min(2.0, multiplier))

    def _drawdown_adjustment(self, current_balance, peak_balance):
        """Reduce size during drawdowns to prevent death spiral.

        Standard hedge fund practice:
        - Drawdown < 3%: full size (1.0x)
        - Drawdown 3-6%: 0.75x
        - Drawdown 6-10%: 0.50x
        - Drawdown > 10%: 0.25x (emergency mode)
        """
        if peak_balance <= 0:
            return 1.0
        drawdown = (peak_balance - current_balance) / peak_balance
        if drawdown < 0.03:
            return 1.0
        elif drawdown < 0.06:
            return 0.75
        elif drawdown < 0.10:
            return 0.50
        else:
            return 0.25

    def _session_adjustment(self, session_stats, current_hour):
        """Boost sizing during historically best trading hours.

        session_stats: dict of {hour: {trades, wins, pnl}}
        Returns: multiplier based on this hour's performance vs average.
        """
        if not session_stats or current_hour not in session_stats:
            return 1.0
        this_hour = session_stats[current_hour]
        if this_hour.get("trades", 0) < 10:
            return 1.0  # Need at least 10 trades in this hour

        # Compare this hour's avg P&L to overall avg
        this_avg = this_hour.get("pnl", 0) / this_hour["trades"]
        all_trades = sum(h.get("trades", 0) for h in session_stats.values())
        all_pnl = sum(h.get("pnl", 0) for h in session_stats.values())
        overall_avg = all_pnl / all_trades if all_trades > 0 else 0

        if overall_avg == 0:
            return 1.0

        # If this hour does 1.5x the avg P&L, size up by 1.2x (capped)
        ratio = this_avg / overall_avg
        if ratio > 1.5:
            return 1.2
        elif ratio > 1.2:
            return 1.1
        elif ratio < 0.5:
            return 0.7
        elif ratio < 0.8:
            return 0.9
        return 1.0

    def calculate(self, balance, price, stop_loss, strategy_allocation=1.0, symbol=None,
                  trade_history=None, peak_balance=None, session_stats=None, current_hour=None):
        """
        Calculate position size using Kelly + drawdown + session-aware sizing.

        Args:
            balance: Current account balance
            price: Entry price
            stop_loss: Stop loss price
            strategy_allocation: Fraction of capital for this strategy (0-1)
            symbol: Ticker symbol (used for crypto-specific sizing)
            trade_history: List of past trades (for Kelly calculation)
            peak_balance: All-time peak balance (for drawdown calc)
            session_stats: Per-hour performance dict
            current_hour: Current hour (for session adjustment)

        Returns:
            int: Number of shares/contracts (0 if trade doesn't meet criteria)
        """
        if price <= 0 or stop_loss <= 0:
            return 0

        # Available capital (after reserve)
        available = balance * (1 - self.reserve_pct)

        # Crypto gets smaller position cap (more volatile)
        position_pct = self.crypto_max_position_pct if self._is_crypto(symbol) else self.max_position_pct

        # Max position value scales with account size
        max_position = min(
            balance * position_pct,
            available
        )

        # === ADAPTIVE RISK CALCULATION ===
        # Base risk, then apply Kelly + drawdown + session multipliers
        base_risk = self.risk_per_trade_pct

        kelly_mult = self._kelly_adjustment(trade_history, base_risk) if trade_history else 1.0
        dd_mult = self._drawdown_adjustment(balance, peak_balance) if peak_balance else 1.0
        session_mult = self._session_adjustment(session_stats, current_hour) if session_stats else 1.0

        adjusted_risk_pct = base_risk * kelly_mult * dd_mult * session_mult

        # Safety floor: never risk more than 3% per trade even if Kelly says more
        adjusted_risk_pct = min(adjusted_risk_pct, 0.03)
        # Safety ceiling: always risk at least 0.25% (don't trade tiny)
        adjusted_risk_pct = max(adjusted_risk_pct, 0.0025)

        if kelly_mult != 1.0 or dd_mult != 1.0 or session_mult != 1.0:
            log.info(
                f"ADAPTIVE SIZING: base={base_risk:.2%} → "
                f"Kelly {kelly_mult:.2f}x × DD {dd_mult:.2f}x × Session {session_mult:.2f}x = "
                f"{adjusted_risk_pct:.2%} risk"
            )

        # Risk per trade in dollars
        risk_dollars = balance * adjusted_risk_pct

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
