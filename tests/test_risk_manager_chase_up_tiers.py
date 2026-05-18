"""Chase-up filter tiers — protects momentum scalps on cheap stocks.

The original chase-up filter rejected any BUY where market > 5% above
signal price (12% in extended hours). That's correct for high-priced
stocks where 5% is a meaningful drift, but it kills the exact cheap-
stock momentum-scalp strategy the user runs: a $1.16 stock breaking
to $1.30 (+12%) is the strategy fire, not a stale signal.

Observed 2026-05-18 02:03 (overnight): CRCD ($2.86 → $3.06), SMST,
and QNCX ($1.16 → $1.30) all rejected for chase-up. The tiered fix:
  - momentum strategy → no chase-up cap (the strategy chases by design)
  - else price < $5 → use absolute $0.50/share cap
  - else → existing percentage cap (5% RTH / 12% extended hours)
"""
from __future__ import annotations

import copy
from datetime import datetime

import pytest

from bot.risk.manager import RiskManager


def _make_signal(symbol, price, market_price, strategy="mean_reversion",
                 extended_hours=False):
    return {
        "symbol": symbol,
        "action": "buy",
        "price": price,
        "market_price": market_price,
        "quantity": 100,
        "stop_loss": price * 0.97,
        "take_profit": price * 1.06,
        "confidence": 0.7,
        "timestamp": datetime.now(),
        "strategy": strategy,
        "_extended_hours": extended_hours,
    }


def test_momentum_strategy_bypasses_chase_up(config):
    """Momentum is meant to chase — even a 30% drift should pass the
    chase-up check (the strategy logic itself decides if the chase is
    too far)."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal("CRCD", price=2.86, market_price=3.72,  # +30%
                       strategy="momentum")
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert passed, f"momentum should bypass chase-up; got: {reason}"


def test_low_price_stock_uses_absolute_cap_passes_small_thrust(config):
    """The QNCX case: $1.16 → $1.30 (+$0.14) is normal first-candle
    momentum on a cheap stock and should be allowed by the $0.50
    absolute cap (even though it's +12% in percentage terms)."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal("QNCX", price=1.16, market_price=1.30,
                       strategy="mean_reversion")
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert passed, f"sub-$5 +$0.14 drift should pass; got: {reason}"


def test_low_price_stock_blocks_big_absolute_jump(config):
    """A $1 stock that's already +$0.60 from signal is a true chase;
    the $0.50 absolute cap binds."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal("XYZ", price=1.00, market_price=1.60,
                       strategy="mean_reversion")
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert not passed
    assert "sub-$5 stocks" in reason


def test_normal_price_stock_keeps_5pct_rth(config):
    """Existing behaviour for non-momentum, non-cheap stocks must be
    preserved. AAPL at $200 → $215 (+7.5%) should still be blocked."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal("AAPL", price=200.00, market_price=215.00,
                       strategy="mean_reversion")
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert not passed
    assert "Chase-up" in reason and "RTH" in reason


def test_normal_price_within_5pct_rth_passes(config):
    """Sanity: 3% drift on a normal-priced stock still allowed."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal("AAPL", price=200.00, market_price=206.00,
                       strategy="mean_reversion")
    sig["quantity"] = 50  # keep notional under position-size cap
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert passed, f"unexpected rejection: {reason}"


def test_momentum_still_blocked_by_chase_DOWN(config):
    """Momentum bypass is only for chase-UP. A falling tape between
    signal and execution must still block momentum (don't buy fades
    just because the strategy is 'momentum')."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal("XYZ", price=10.00, market_price=9.40,  # -6%
                       strategy="momentum")
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert not passed
    assert "Setup broke" in reason
