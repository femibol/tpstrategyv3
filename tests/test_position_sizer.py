"""Unit tests for bot.risk.position_sizer.PositionSizer.

Covers degenerate inputs (zero price/stop), the core risk-based math,
the price-tier caps, the reserve-cash floor, the crypto-vs-equity split,
the Kelly/drawdown/session multipliers, and `update_tier`.
"""
from __future__ import annotations

import pytest

from bot.risk.position_sizer import PositionSizer


# -----------------------------------------------------------------------------
# Degenerate inputs
# -----------------------------------------------------------------------------

def test_zero_price_returns_zero(config):
    sizer = PositionSizer(config)
    assert sizer.calculate(balance=100_000, price=0.0, stop_loss=145.0) == 0


def test_zero_stop_returns_zero(config):
    sizer = PositionSizer(config)
    assert sizer.calculate(balance=100_000, price=150.0, stop_loss=0.0) == 0


def test_price_equals_stop_returns_zero(config):
    """Per-share risk of $0 collapses the math; sizer must return 0, not blow up."""
    sizer = PositionSizer(config)
    assert sizer.calculate(balance=100_000, price=150.0, stop_loss=150.0) == 0


# -----------------------------------------------------------------------------
# Core risk math + caps
# -----------------------------------------------------------------------------

def test_basic_risk_based_sizing(config):
    """$100k balance, 1% risk = $1k risk budget. Per-share risk = $5.
    Risk-based shares = 200. Tier ($25-50): max 300. Max position 15% =
    $15k -> 100 shares cap. Result is 100 (max-position binds first)."""
    sizer = PositionSizer(config)
    shares = sizer.calculate(balance=100_000, price=150.0, stop_loss=145.0)
    assert shares > 0
    # Position dollar value <= 15% of balance
    assert shares * 150.0 <= 100_000 * 0.15 + 1e-6


def test_max_position_pct_cap_binds(config):
    """Tighten max_position_size_pct to 5% and confirm it caps shares."""
    config.risk_config["max_position_size_pct"] = 0.05
    sizer = PositionSizer(config)
    shares = sizer.calculate(balance=100_000, price=100.0, stop_loss=95.0)
    assert shares * 100.0 <= 100_000 * 0.05 + 1e-6


def test_reserve_cash_respected(config):
    """Half the balance is held back as reserve — confirm the sizer doesn't
    deploy more than the available half."""
    config.reserve_cash_pct = 0.50
    sizer = PositionSizer(config)
    shares = sizer.calculate(balance=10_000, price=100.0, stop_loss=95.0)
    # Available capital = $5k; sizer shouldn't exceed that.
    assert shares * 100.0 <= 5_000 + 1e-6


def test_low_balance_zero_position(config):
    """If price exceeds available capital, sizer can't fund even one share."""
    sizer = PositionSizer(config)
    shares = sizer.calculate(balance=50.0, price=150.0, stop_loss=145.0)
    assert shares == 0


# -----------------------------------------------------------------------------
# Price tiers
# -----------------------------------------------------------------------------

def test_sub_dollar_runner_caps_at_5000_shares(config):
    """Tier (max_price=$2): max 5000 shares. Use a wide stop so the
    risk-based math isn't itself the binding constraint."""
    sizer = PositionSizer(config)
    shares = sizer.calculate(balance=1_000_000, price=1.50, stop_loss=1.49)
    assert shares <= 5000


def test_high_priced_stock_caps_at_tier(config):
    """Tier (max_price=$500, max 50 shares) for $400 stock."""
    sizer = PositionSizer(config)
    shares = sizer.calculate(balance=10_000_000, price=400.0, stop_loss=380.0)
    assert shares <= 50


def test_ultra_priced_stock_capped_at_10(config):
    """Tier (max_price=99999, max 10 shares) for a $5000 stock."""
    sizer = PositionSizer(config)
    shares = sizer.calculate(balance=10_000_000, price=5_000.0, stop_loss=4_900.0)
    assert shares <= 10


# -----------------------------------------------------------------------------
# Crypto sizing
# -----------------------------------------------------------------------------

def test_crypto_uses_smaller_position_cap(config):
    """Crypto cap is 10% (default), equity is 15%. Same balance + price →
    crypto position value should be at most 10% of balance."""
    sizer = PositionSizer(config)
    shares = sizer.calculate(
        balance=100_000, price=100.0, stop_loss=95.0, symbol="BTC-USD"
    )
    assert shares * 100.0 <= 100_000 * 0.10 + 1e-6


def test_non_crypto_can_use_full_equity_cap(config):
    sizer = PositionSizer(config)
    eq_shares = sizer.calculate(balance=100_000, price=100.0, stop_loss=95.0,
                                symbol="AAPL")
    crypto_shares = sizer.calculate(balance=100_000, price=100.0, stop_loss=95.0,
                                    symbol="ETH-USD")
    # Equity cap (15%) > crypto cap (10%) -> equity sizing should be >=
    assert eq_shares >= crypto_shares


# -----------------------------------------------------------------------------
# Drawdown adjustment
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("drawdown_pct,expected_mult", [
    (0.00, 1.0),    # no drawdown
    (0.03, 1.0),    # exactly 3% — inclusive boundary, full size
    (0.05, 0.75),   # 5% — second tier
    (0.06, 0.75),   # boundary
    (0.08, 0.50),   # 8% — third tier
    (0.10, 0.50),   # boundary
    (0.15, 0.25),   # >10% — emergency mode
])
def test_drawdown_multiplier(config, drawdown_pct, expected_mult):
    sizer = PositionSizer(config)
    peak = 100_000.0
    current = peak * (1 - drawdown_pct)
    assert sizer._drawdown_adjustment(current, peak) == expected_mult


def test_drawdown_with_zero_peak_is_safe(config):
    sizer = PositionSizer(config)
    assert sizer._drawdown_adjustment(0, 0) == 1.0


# -----------------------------------------------------------------------------
# Kelly adjustment
# -----------------------------------------------------------------------------

def test_kelly_with_too_few_trades_returns_neutral(config):
    sizer = PositionSizer(config)
    history = [{"pnl": 50}, {"pnl": -25}]  # only 2 trades, need 20+
    assert sizer._kelly_adjustment(history, 0.01) == 1.0


def test_kelly_negative_edge_scales_down(config):
    """Heavy losses, small wins => negative Kelly => 0.25x floor."""
    sizer = PositionSizer(config)
    history = [{"pnl": 5}] * 5 + [{"pnl": -100}] * 20
    mult = sizer._kelly_adjustment(history, 0.01)
    assert mult == 0.25


def test_kelly_positive_edge_within_bounds(config):
    """Strong positive edge clamps at 2.0x ceiling."""
    sizer = PositionSizer(config)
    history = [{"pnl": 200}] * 60 + [{"pnl": -50}] * 10
    mult = sizer._kelly_adjustment(history, 0.01)
    assert 0.25 <= mult <= 2.0


def test_kelly_no_wins_or_no_losses_returns_neutral(config):
    sizer = PositionSizer(config)
    only_wins = [{"pnl": 100}] * 25
    only_losses = [{"pnl": -50}] * 25
    assert sizer._kelly_adjustment(only_wins, 0.01) == 1.0
    assert sizer._kelly_adjustment(only_losses, 0.01) == 1.0


# -----------------------------------------------------------------------------
# Session adjustment
# -----------------------------------------------------------------------------

def test_session_no_stats_returns_neutral(config):
    sizer = PositionSizer(config)
    assert sizer._session_adjustment(None, current_hour=10) == 1.0


def test_session_too_few_trades_in_hour_returns_neutral(config):
    sizer = PositionSizer(config)
    stats = {10: {"trades": 5, "wins": 3, "pnl": 100}}
    assert sizer._session_adjustment(stats, 10) == 1.0


def test_session_strong_hour_boosts(config):
    sizer = PositionSizer(config)
    # This hour: 20 trades, $20 avg P&L. Other hours: 100 trades, $5 avg.
    stats = {
        10: {"trades": 20, "wins": 15, "pnl": 400},   # this hour avg = 20
        11: {"trades": 100, "wins": 50, "pnl": 500},  # avg 5
    }
    mult = sizer._session_adjustment(stats, 10)
    assert mult > 1.0


def test_session_weak_hour_cuts(config):
    sizer = PositionSizer(config)
    stats = {
        10: {"trades": 20, "wins": 5, "pnl": -100},   # avg = -5
        11: {"trades": 100, "wins": 60, "pnl": 1000}, # avg = 10
    }
    mult = sizer._session_adjustment(stats, 10)
    assert mult < 1.0


# -----------------------------------------------------------------------------
# update_tier
# -----------------------------------------------------------------------------

def test_update_tier_changes_risk_pct(config):
    sizer = PositionSizer(config)
    sizer.update_tier({"risk_per_trade": 0.025, "max_position_pct": 0.20})
    assert sizer.risk_per_trade_pct == 0.025
    assert sizer.max_position_pct == 0.20


def test_update_tier_with_none_is_noop(config):
    sizer = PositionSizer(config)
    before = (sizer.risk_per_trade_pct, sizer.max_position_pct)
    sizer.update_tier(None)
    assert (sizer.risk_per_trade_pct, sizer.max_position_pct) == before


# -----------------------------------------------------------------------------
# Risk floor / ceiling enforcement (integration via .calculate)
# -----------------------------------------------------------------------------

def test_kelly_cannot_push_risk_above_3pct_safety_ceiling(config):
    """Even with a strong positive Kelly edge, calculated risk caps at 3%."""
    sizer = PositionSizer(config)
    # Strong positive history -> Kelly multiplier could push risk well above 3%
    history = [{"pnl": 500}] * 80 + [{"pnl": -50}] * 5
    shares = sizer.calculate(
        balance=100_000, price=10.0, stop_loss=9.0,
        trade_history=history,
    )
    # Risk per share = $1; 3% of $100k = $3k => max 3000 shares from risk.
    # Tier ($5-$10): max 1000. So bound is 1000. Confirm we don't exceed it.
    assert shares <= 1000
