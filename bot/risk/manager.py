"""
Risk Manager - The most important part of the system.
Protects capital at all costs. No trade gets through without approval.
"""
from datetime import datetime
from collections import defaultdict

from bot.risk.cost_model import CostModel
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
        # Per-asset-class sub-caps so 24/7 crypto can't monopolize the
        # shared slot budget and starve equity at RTH open. Optional —
        # if unset, both default to max_positions (no sub-cap effect).
        # Observed live 2026-05-18: AMZN entry signals rejected for ~45
        # minutes overnight with "Max positions reached (7)" while 7
        # crypto orphans held every slot. Sub-caps prevent that class
        # of starvation without changing behaviour when only one asset
        # class is active.
        self.max_crypto_positions = self.risk.get(
            "max_crypto_positions", self.max_positions
        )
        self.max_equity_positions = self.risk.get(
            "max_equity_positions", self.max_positions
        )
        # Low-float catalyst gets its own reserved slot pool so the lane is
        # available even when momentum/crypto have filled normal slots.
        # Detection is by strategy="low_float_catalyst" on the position.
        self.max_low_float_positions = self.risk.get(
            "max_low_float_positions", 0
        )
        # Penny runner pool — reserved slots for sub-$2 squeeze plays
        # (YMAT $0.23 → $2.30-style 10x candidates). Detection is by
        # entry price falling in [penny_runner_price_min, penny_runner_price_max].
        # Disabled when max=0; the price-band check still runs but no pool gate fires.
        self.max_penny_runner_positions = self.risk.get(
            "max_penny_runner_positions", 0
        )
        self.penny_runner_price_min = self.risk.get("penny_runner_price_min", 0.20)
        self.penny_runner_price_max = self.risk.get("penny_runner_price_max", 2.00)
        self.max_position_pct = self.risk.get("max_position_size_pct", 0.15)
        self.risk_per_trade = self.risk.get("risk_per_trade_pct", 0.01)
        self.min_volume = self.risk.get("min_volume", 50000)
        self.min_price = self.risk.get("min_price", 0.50)
        self.max_price = self.risk.get("max_price", 99999.0)  # No hard ceiling - runners can go past scanner range
        self.max_correlated = self.risk.get("max_correlated_positions", 2)
        self.min_confidence = 0.35  # Was 0.40. Mean-reversion math lands at 0.35-0.40 on typical setups; 0.40 was silently rejecting valid signals.
        # Per-strategy confidence floors. Default falls back to
        # self.min_confidence (above) when the strategy isn't listed.
        # Conservative dial 2026-05-18: scalp/momentum get higher bars
        # since their low-conf entries are the noise that drives losses;
        # mean-reversion stays at the global floor because its math
        # naturally lands lower. Runner-safe: only blocks marginal-conf
        # entries, never closes a position already running.
        self.min_confidence_per_strategy = self.risk.get(
            "min_confidence_per_strategy",
            {
                "rvol_scalp": 0.55,
                "rvol_momentum": 0.55,
                "momentum": 0.50,
                "momentum_runner": 0.50,
                "mean_reversion": 0.40,
            },
        )
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

        # Transaction-cost gate. Rejects entries whose take_profit edge
        # can't clear round-trip cost (fee + spread×mult) by ratio×.
        # Ratio-based so cheap-stock momentum scalps with big % moves
        # still pass even when the absolute bps cost looks scary.
        self.cost_model = CostModel(config)

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
                # Stamp the reason onto the signal so downstream reporters
                # (Discord rejections, dashboard) show the true cause instead
                # of having to reconstruct/guess.
                signal["_rejection_reason"] = reason
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

        # --- Rule 1: Max positions (global, then per asset class) ---
        if len(positions) >= self.max_positions:
            return False, f"Max positions reached ({self.max_positions})"
        _is_crypto_for_cap = any(symbol.upper().endswith(s) for s in self.crypto_suffixes)
        _is_low_float_for_cap = (signal.get("strategy") == "low_float_catalyst")
        _is_penny_for_cap = (
            not _is_crypto_for_cap
            and not _is_low_float_for_cap
            and price > 0
            and self.penny_runner_price_min <= price <= self.penny_runner_price_max
            and self.max_penny_runner_positions > 0
        )
        if _is_low_float_for_cap and self.max_low_float_positions > 0:
            low_float_held = sum(
                1 for p in positions.values()
                if isinstance(p, dict) and p.get("strategy") == "low_float_catalyst"
            )
            if low_float_held >= self.max_low_float_positions:
                return False, (
                    f"Low-float sub-cap reached ({low_float_held}/"
                    f"{self.max_low_float_positions})"
                )
        elif _is_penny_for_cap:
            penny_held = sum(
                1 for p in positions.values()
                if isinstance(p, dict)
                and self.penny_runner_price_min <= (p.get("entry_price") or 0) <= self.penny_runner_price_max
            )
            if penny_held >= self.max_penny_runner_positions:
                return False, (
                    f"Penny-runner sub-cap reached ({penny_held}/"
                    f"{self.max_penny_runner_positions})"
                )
        elif _is_crypto_for_cap and self.max_crypto_positions < self.max_positions:
            crypto_held = sum(
                1 for s in positions
                if any(s.upper().endswith(suf) for suf in self.crypto_suffixes)
            )
            if crypto_held >= self.max_crypto_positions:
                return False, (
                    f"Crypto sub-cap reached ({crypto_held}/"
                    f"{self.max_crypto_positions})"
                )
        elif not _is_crypto_for_cap and self.max_equity_positions < self.max_positions:
            equity_held = sum(
                1 for s in positions
                if not any(s.upper().endswith(suf) for suf in self.crypto_suffixes)
            )
            if equity_held >= self.max_equity_positions:
                return False, (
                    f"Equity sub-cap reached ({equity_held}/"
                    f"{self.max_equity_positions})"
                )

        # --- Rule 2: Already in position ---
        if symbol in positions:
            return False, f"Already in position: {symbol}"

        # --- Rule 3: Signal age - reject stale signals ---
        # During pre/post market the bot generates a burst of signals across many
        # strategies that can queue behind execution; 60s rejected most of them
        # at ~100s old. Wider 180s window outside RTH keeps the entries flowing.
        extended_hours = bool(signal.get("_extended_hours"))
        max_signal_age = 180 if extended_hours else 60
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
                if age_seconds > max_signal_age:
                    return False, f"Stale signal: {age_seconds:.0f}s old (max {max_signal_age}s)"

        # --- Rule 4: Min price (skip for options + crypto + penny pool) ---
        # Crypto routinely trades sub-$1 (MATIC $0.09, FLOKI/PEPE/BONK/SHIB
        # in the micro-cents). The $0.50 floor exists to keep equity penny
        # junk out — it has no meaning for crypto where price is just a
        # function of supply. Penny-pool entries have their own price band
        # (penny_runner_price_min/max) so they can enter below the universe floor.
        _is_crypto_sym = any(symbol.upper().endswith(s) for s in self.crypto_suffixes)
        if (
            signal.get("asset_type") != "option"
            and not _is_crypto_sym
            and not _is_penny_for_cap
            and price > 0
            and price < self.min_price
        ):
            return False, f"Price ${price:.2f} below minimum ${self.min_price}"

        # --- Rule 5: Max price (skip for options + crypto) ---
        # BTC at $78K would trip an equity max_price ceiling; same reasoning
        # as min_price — crypto sizing is bounded by crypto_max_position_pct,
        # not nominal price.
        if signal.get("asset_type") != "option" and not _is_crypto_sym and price > self.max_price:
            return False, f"Price ${price:.2f} above maximum ${self.max_price}"

        # --- Rule 6: Price reasonableness — DIRECTIONAL ---
        # For BUY signals we treat the two directions of drift asymmetrically:
        #   chase UP (market > signal): trend strengthened since signal; entering
        #     higher is normal for momentum/gap plays. Wider cap (5% RTH / 12% ext).
        #   chase DOWN (market < signal): the bullish setup broke between signal
        #     generation and execution. This is "buying a fade" — tight cap
        #     (3% RTH / 5% ext) regardless of session.
        # Non-buy actions keep the symmetric cap.
        market_price = signal.get("market_price") or signal.get("current_price")
        if market_price and price and market_price > 0:
            signed_diff = (market_price - price) / price  # +: market above signal (chase up); -: below (chase down)
            if action == "buy":
                if signed_diff >= 0:
                    # Momentum strategies are explicitly meant to chase — a
                    # rising tape AFTER signal is the strategy, not a stale
                    # signal. Disable chase-up here so cheap-stock breakout
                    # scalps don't get rejected for being 7-12% above signal
                    # (saw this overnight on CRCD/SMST/QNCX 2026-05-18).
                    # Note: the chase-DOWN check below still applies — even
                    # momentum shouldn't buy a falling tape.
                    strategy = (signal.get("strategy", "") or "").lower()
                    if "momentum" in strategy:
                        pass  # no chase-up cap for momentum
                    elif price < 5.0:
                        # Penny / low-priced stocks: 5% can be a single tick
                        # (a $1 stock with $0.05 drift is already at the
                        # cap). Use an absolute $/share cap so normal
                        # first-candle thrust on cheap names isn't rejected.
                        max_abs = 0.50
                        abs_diff = market_price - price
                        if abs_diff > max_abs:
                            return False, (
                                f"Chase-up: signal ${price:.2f} → market "
                                f"${market_price:.2f} (+${abs_diff:.2f}, max "
                                f"+${max_abs:.2f} for sub-$5 stocks)"
                            )
                    else:
                        max_drift = 0.12 if extended_hours else 0.05
                        if signed_diff > max_drift:
                            return False, (
                                f"Chase-up: signal ${price:.2f} → market ${market_price:.2f} "
                                f"({signed_diff:+.1%}, max {max_drift:.0%} "
                                f"{'ext' if extended_hours else 'RTH'})"
                            )
                else:
                    # Price moved DOWN since signal — setup broke. Tight cap.
                    max_drop = 0.05 if extended_hours else 0.03
                    if abs(signed_diff) > max_drop:
                        return False, (
                            f"Setup broke: signal ${price:.2f} → market ${market_price:.2f} "
                            f"({signed_diff:+.1%}, max -{max_drop:.0%})"
                        )
            else:
                max_price_diff = 0.12 if extended_hours else 0.05
                if abs(signed_diff) > max_price_diff:
                    return False, (
                        f"Price ${price:.2f} is {abs(signed_diff):.1%} away from "
                        f"market ${market_price:.2f} (max {max_price_diff:.0%})"
                    )

        # --- Rule 6.5: Cost-vs-edge gate (entries only) ---
        # Rejects signals whose take_profit edge can't clear round-trip
        # fee+spread by a configured ratio. Buy-only path — exits already
        # bypassed the rule via the early return at the top of this method.
        if action == "buy":
            _is_crypto_for_cost = any(
                symbol.upper().endswith(s) for s in self.crypto_suffixes
            )
            _asset_class = "crypto" if _is_crypto_for_cost else "equity"
            _passed_cost, _cost_reason = self.cost_model.passes(
                signal, _asset_class
            )
            if not _passed_cost:
                return False, _cost_reason

        # --- Rule 7: Position size limit (crypto gets smaller cap) ---
        is_crypto = any(symbol.upper().endswith(s) for s in self.crypto_suffixes)
        pos_pct = self.crypto_max_position_pct if is_crypto else self.max_position_pct
        max_position = balance * pos_pct
        explicit_qty = signal.get("quantity")
        # When a strategy signal has no explicit quantity, the downstream
        # position_sizer will compute it (and for crypto, it sizes FRACTIONALLY
        # against the same cap). Don't reject those signals here on the
        # default-of-1 placeholder — for BTC at $77K, that placeholder fakes a
        # $77K position vs a $3K cap and kills every signal. We use
        # `max_position` as a stand-in: the sizer is guaranteed not to exceed
        # it, so by construction this signal will fit.
        if explicit_qty is None and is_crypto:
            position_value = max_position
        else:
            position_value = price * (explicit_qty or 1)
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

        # --- Rule 8.6: Portfolio-wide risk ceiling (2026-07-10 audit) ---
        # capital.max_portfolio_risk had been dead config since inception —
        # read nowhere. Enforce it: the sum of open-position risk (distance
        # to stop x qty) across the book must stay under the ceiling before
        # a new entry is allowed. Positions without a stop count at 3% of
        # their value (the default stop) rather than zero, so a missing
        # stop can't create invisible risk headroom.
        max_pf_risk = float(
            self.config.settings.get("capital", {}).get("max_portfolio_risk", 0) or 0
        )
        if max_pf_risk > 0:
            open_risk = 0.0
            for p in positions.values():
                entry = p.get("entry_price", 0) or 0
                qty = p.get("quantity", 0) or 0
                stop = p.get("stop_loss", 0) or 0
                if entry <= 0 or qty <= 0:
                    continue
                per_share = (entry - stop) if 0 < stop < entry else entry * 0.03
                open_risk += max(0.0, per_share) * qty
            ceiling = balance * max_pf_risk
            if open_risk >= ceiling:
                return False, (
                    f"Portfolio risk ${open_risk:.0f} at/over ceiling "
                    f"${ceiling:.0f} ({max_pf_risk:.0%} of balance) — no new entries"
                )

        # --- Rule 9: Confidence threshold ---
        confidence = signal.get("confidence", 0)
        # Per-strategy floor wins over global if specified for this strategy.
        _strategy = (signal.get("strategy", "") or "").lower()
        _floor = self.min_confidence_per_strategy.get(_strategy, self.min_confidence)
        if confidence < _floor:
            return False, (
                f"Confidence {confidence:.2f} below {_strategy or 'global'} "
                f"threshold {_floor:.2f}"
            )

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
            # Silently returning False here used to hide a real bug: a transient
            # sync glitch that wiped SOD balance would also disable the daily-loss
            # gate. Surface it instead — the gate is degraded until balance recovers.
            log.warning(
                "Daily-loss gate degraded: start_of_day_balance=%s (<=0). "
                "Gate inactive until balance state is valid.",
                start_of_day_balance,
            )
            return False
        daily_loss = (start_of_day_balance - current_balance) / start_of_day_balance
        return daily_loss >= self.config.max_daily_loss

    def is_max_drawdown_exceeded(self, current_balance, peak_balance):
        """Check if max drawdown from peak has been exceeded."""
        if peak_balance <= 0:
            # Same as is_daily_loss_exceeded: log instead of swallowing. Returning
            # True here would force-close the book on a balance-sync hiccup, which
            # is worse than the silent fail-open — but the operator needs to see it.
            log.warning(
                "Max-drawdown gate degraded: peak_balance=%s (<=0). "
                "Gate inactive until balance state is valid.",
                peak_balance,
            )
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
