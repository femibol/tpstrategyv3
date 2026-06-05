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
        # Upper bound on the per-strategy Kelly multiplier. Ramp knob: start
        # at 1.0 (a proven strategy sizes at base risk, no Kelly boost) and
        # raise toward 2.0 once the per-strategy edge is trusted live.
        self.kelly_max_mult = config.risk_config.get("kelly_max_mult", 2.0)

        # Crypto-specific limits
        crypto_risk = config.settings.get("crypto", {}).get("risk", {})
        self.crypto_max_position_pct = crypto_risk.get("max_position_size_pct", 0.10)
        self.crypto_suffixes = config.settings.get("crypto", {}).get(
            "symbols_suffix", ["-USD", "-USDT", "-BTC", "-ETH"]
        )
        # Penny-runner pool: tight position cap so a -50% blowup on a lottery
        # ticket doesn't sink the day. Detection is by entry price band.
        self.penny_runner_max_position_pct = config.risk_config.get(
            "penny_runner_max_position_size_pct", 0.05
        )
        self.penny_runner_price_min = config.risk_config.get("penny_runner_price_min", 0.20)
        self.penny_runner_price_max = config.risk_config.get("penny_runner_price_max", 15.00)

        # Per-strategy absolute dollar cap on risk-per-trade. Resolves the
        # mean_reversion asymmetry: 5% min stop × $2,872 position = -$143
        # max risk vs +$15 typical win = 9.5:1 loss/win ratio that erases
        # weekly gains on a single bad entry (see 2026-06-04 NEAR-USD trade,
        # -$127.94 in 77 minutes that wiped Tue + Wed combined).
        #
        # Applied AFTER all multiplicative sizing (Kelly/DD/Session/etc.) so
        # this is the final ceiling on risk_dollars. Position size shrinks
        # accordingly: at $50 cap with 5% stop, position is $1,000 not
        # $2,872 — same trade fires, same stop %, but max loss bounded.
        #
        # Conservative defaults — only mean_reversion is currently capped
        # because that's where the 200-trade audit showed the asymmetry.
        # Extend per strategy as data warrants. Set value to 0 / null to
        # disable for a strategy.
        self.max_dollar_risk_per_strategy = (
            config.risk_config.get("max_dollar_risk_per_strategy", {}) or {}
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

    def _kelly_adjustment(self, trade_history, base_risk_pct, strategy=None):
        """Calculate risk multiplier using Risk-Constrained Kelly Criterion.

        Kelly = (W*B - L) / B
          where W = win rate, L = loss rate, B = avg_win/avg_loss ratio

        We use HALF-KELLY (k/2) for safety — full Kelly is too volatile.
        Bounded to [0.25x, 2.0x] of base risk to prevent extreme sizing.

        Per-strategy: when ``strategy`` is given, Kelly is computed on that
        strategy's OWN trade record. A blended portfolio history lets one
        strategy's losers drag a proven winner down to the 0.25x floor —
        on 2026-05-21 the blended last-100 was 46% WR / negative edge
        (→ 0.25x for everything) while mean_reversion alone ran 61% WR with
        a positive edge (→ 2.0x). Per-strategy Kelly is the mechanism for
        "more aggressive on what works, defensive on what doesn't." A
        strategy with fewer than 20 of its own trades is treated as
        unproven and sized neutral (1.0x).

        Returns: multiplier (float) to apply to base risk_per_trade_pct.
        """
        if not trade_history:
            return 1.0

        hist = list(trade_history)
        if strategy:
            strat_hist = [t for t in hist if t.get("strategy") == strategy]
            if len(strat_hist) < 20:
                # Unproven strategy — neither reward nor penalize.
                return 1.0
            hist = strat_hist
        elif len(hist) < 20:
            # Need at least 20 trades for statistical significance
            return 1.0

        recent = hist[-100:]  # Last 100 trades for rolling Kelly
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

        # Bound: never below 0.25x, never above the configured ramp ceiling
        # (kelly_max_mult — 1.0 while ramping in, up to 2.0 once trusted).
        return max(0.25, min(self.kelly_max_mult, multiplier))

    def _drawdown_adjustment(self, current_balance, peak_balance):
        """Reduce size during drawdowns to prevent death spiral.

        Standard hedge fund practice (inclusive-upper tier boundaries):
        - Drawdown 0-3%: full size (1.0x)
        - Drawdown 3-6%: 0.75x
        - Drawdown 6-10%: 0.50x
        - Drawdown > 10%: 0.25x (emergency mode)

        Uses <= for upper bounds so exactly 3% stays in the full-size tier,
        only dropping to 0.75x after crossing 3% (avoids premature cuts).
        """
        if peak_balance <= 0:
            return 1.0
        drawdown = (peak_balance - current_balance) / peak_balance
        if drawdown <= 0.03:
            return 1.0
        elif drawdown <= 0.06:
            return 0.75
        elif drawdown <= 0.10:
            return 0.50
        else:
            return 0.25

    @staticmethod
    def _confidence_multiplier(confidence):
        """Scale risk by signal confidence — bet bigger when the bot is most
        confident, smaller when it's a marginal setup.

        Buckets (piecewise, not interpolated, to keep behavior predictable):
          >= 0.85: 1.5x  (high conviction — A+ setups)
          >= 0.70: 1.2x  (above average)
          >= 0.55: 1.0x  (neutral — most signals land here)
          else:    0.7x  (low conviction; risk_manager already rejects <0.35)
        """
        if confidence is None:
            return 1.0
        if confidence >= 0.85:
            return 1.5
        if confidence >= 0.70:
            return 1.2
        if confidence >= 0.55:
            return 1.0
        return 0.7

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
                  trade_history=None, peak_balance=None, session_stats=None, current_hour=None,
                  confidence=None, regime_multiplier=1.0, vol_regime_mult=1.0, strategy=None):
        """
        Calculate position size using Kelly + drawdown + session + confidence
        + regime multipliers stacked on top of base risk_per_trade_pct.

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
            confidence: Signal confidence 0-1 — high-conviction setups get
                bigger size (1.5x at 0.85+), marginal ones smaller (0.7x).
            regime_multiplier: Per-strategy regime affinity (from
                REGIME_STRATEGY_AFFINITY) — already used for EOD allocation,
                now applied per-signal so live conditions move sizing.
            vol_regime_mult: Realized-vol regime dampener. Clamped to
                [0.4, 1.0] — can size DOWN when vol spikes vs baseline,
                never UP. Caller computes from short/long realized-vol
                ratio (engine._compute_vol_regime_mult).
            strategy: Strategy name. When set, Kelly is computed on that
                strategy's own trade record instead of the blended
                portfolio (see _kelly_adjustment).

        Returns:
            int: Number of shares/contracts (0 if trade doesn't meet criteria)
        """
        if price <= 0 or stop_loss <= 0:
            return 0

        # Available capital (after reserve)
        available = balance * (1 - self.reserve_pct)

        # Per-class position cap: crypto smaller (volatile), penny-runner smallest
        # (lottery-ticket risk). Falls back to standard equity cap otherwise.
        if self._is_crypto(symbol):
            position_pct = self.crypto_max_position_pct
        elif self.penny_runner_price_min <= price <= self.penny_runner_price_max:
            position_pct = self.penny_runner_max_position_pct
        else:
            position_pct = self.max_position_pct

        # Max position value scales with account size
        max_position = min(
            balance * position_pct,
            available
        )

        # === ADAPTIVE RISK CALCULATION ===
        # Base risk, then apply Kelly + drawdown + session multipliers
        base_risk = self.risk_per_trade_pct

        kelly_mult = self._kelly_adjustment(trade_history, base_risk, strategy) if trade_history else 1.0
        dd_mult = self._drawdown_adjustment(balance, peak_balance) if peak_balance else 1.0
        session_mult = self._session_adjustment(session_stats, current_hour) if session_stats else 1.0
        conf_mult = self._confidence_multiplier(confidence)
        # Clamp regime multiplier to [0.3, 2.0] so a bad lookup or extreme regime
        # affinity can't push sizing outside sane bounds.
        regime_mult = max(0.3, min(2.0, regime_multiplier or 1.0))
        # Vol regime dampener: 0.4 floor (extreme vol → 60% size cut max),
        # 1.0 ceiling (never sizes UP, only down — protective only).
        vol_mult = max(0.4, min(1.0, vol_regime_mult or 1.0))

        adjusted_risk_pct = (
            base_risk * kelly_mult * dd_mult * session_mult * conf_mult * regime_mult * vol_mult
        )

        # Safety ceiling: never risk more than 2% per trade even if the
        # stacked multipliers say more. With base 1% and per-strategy Kelly
        # capped at 2.0x, a proven strategy reaches exactly 2% on its own
        # edge; the remaining multipliers can only trim from there.
        adjusted_risk_pct = min(adjusted_risk_pct, 0.02)
        # Safety floor: always risk at least 0.25% (don't trade tiny)
        adjusted_risk_pct = max(adjusted_risk_pct, 0.0025)

        if (kelly_mult != 1.0 or dd_mult != 1.0 or session_mult != 1.0
                or conf_mult != 1.0 or regime_mult != 1.0 or vol_mult != 1.0):
            log.info(
                f"ADAPTIVE SIZING: base={base_risk:.2%} → "
                f"Kelly[{strategy or 'all'}] {kelly_mult:.2f}x × DD {dd_mult:.2f}x × "
                f"Session {session_mult:.2f}x × Conf {conf_mult:.2f}x × "
                f"Regime {regime_mult:.2f}x × Vol {vol_mult:.2f}x "
                f"= {adjusted_risk_pct:.2%} risk"
            )

        # Risk per trade in dollars
        risk_dollars = balance * adjusted_risk_pct

        # Per-strategy absolute risk cap. See __init__ docstring for the
        # mean_reversion asymmetry that triggered this. Capping risk_dollars
        # here shrinks the share count below; the strategy's own gates
        # remain in force, only the position SIZE on the resulting trade
        # changes.
        strat_cap = self.max_dollar_risk_per_strategy.get(strategy) if strategy else None
        if strat_cap and strat_cap > 0 and risk_dollars > strat_cap:
            log.info(
                f"STRATEGY RISK CAP: {strategy} risk ${risk_dollars:.2f} → "
                f"${strat_cap:.2f} (per-strategy ceiling)"
            )
            risk_dollars = float(strat_cap)

        # Per-share risk
        per_share_risk = abs(price - stop_loss)
        if per_share_risk <= 0:
            return 0

        # --- Crypto: fractional sizing ---
        # BTC at $77K with a $3K cap floors to 0 whole units; the integer-share
        # math below makes high-priced crypto un-tradable. Take a separate path
        # that keeps quantity as a float quantized to 5 decimals (the precision
        # TradersPost's crypto subscriptions accept for BTC/ETH/SOL).
        if self._is_crypto(symbol):
            qty_by_risk = risk_dollars / per_share_risk
            qty_by_max = max_position / price
            qty = round(min(qty_by_risk, qty_by_max), 5)
            # Dust filter: don't bother trading <$10 notional
            if qty * price < 10:
                log.warning(
                    f"Position size ~0 for crypto {symbol} @ ${price:.2f}: "
                    f"qty={qty} × price={price:.2f} < $10 dust threshold "
                    f"(risk ${risk_dollars:.2f}, max_pos ${max_position:.2f})"
                )
                return 0
            log.info(
                f"Position size (crypto): {qty} {symbol} @ ${price:.2f} = "
                f"${qty * price:,.2f} | Risk: ${qty * per_share_risk:.2f} "
                f"({qty * per_share_risk / balance:.1%} of account)"
            )
            return qty

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
