"""Per-strategy dollar-risk cap on position sizing.

2026-06-04 trade audit: mean_reversion's 5% min stop on a $2,872 NEAR-USD
position = -$143 max risk per trade. Average mean_reversion win is +$15.
9.5:1 loss/win asymmetry — one bad entry wipes 10 average wins. The
NEAR-USD trade at 02:13 UTC on 2026-06-04 lost -$127.94 in 77 minutes,
single-handedly turning the entire week red.

Fix: per-strategy absolute dollar cap on risk_dollars in PositionSizer.
Applied AFTER all multiplicative sizing (Kelly/DD/Session/conf/regime)
so it's the final ceiling. The strategy's own gates remain in force —
only the position SIZE on the resulting trade shrinks.

Config: `risk.max_dollar_risk_per_strategy: {mean_reversion: 50, ...}`.
Empty dict / missing key = disabled (current behavior).

These tests pin five guarantees:
  1. Capped strategy: when raw risk exceeds cap, risk shrinks to cap
  2. Capped strategy: position size shrinks proportionally
  3. Uncapped strategy: behavior unchanged (no regression)
  4. Below-cap risk: behavior unchanged
  5. NEAR-USD-class scenario: bounded loss
"""
from __future__ import annotations

from types import SimpleNamespace


def _make_sizer(strategy_caps=None):
    """Position sizer with deterministic config (no Kelly history, no DD)."""
    from bot.risk.position_sizer import PositionSizer

    cfg = SimpleNamespace(
        risk_per_trade=0.01,
        reserve_cash_pct=0.05,
        settings={"crypto": {}},
        risk_config={
            "max_position_size_pct": 0.15,
            "kelly_max_mult": 2.0,
            "max_dollar_risk_per_strategy": strategy_caps or {},
        },
    )
    return PositionSizer(cfg)


def test_uncapped_strategy_unchanged():
    """A strategy with no entry in max_dollar_risk_per_strategy must
    behave exactly as before the cap was added. ($20 stock avoids the
    penny-runner band that extends through $15.)"""
    sizer = _make_sizer(strategy_caps={})  # no caps
    qty = sizer.calculate(
        balance=10_000, price=20.0, stop_loss=19.0,  # 5% stop
        strategy="momentum",
    )
    # Risk_dollars = 10000 × 0.01 = $100. per_share_risk = $1.
    # shares_by_risk = 100. max_position cap = $1500/$20 = 75 shares.
    # Final: min(100, 75) = 75 shares.
    assert qty == 75


def test_capped_strategy_below_cap_unchanged():
    """If the strategy IS capped but risk_dollars is below the cap, no
    change — cap is a ceiling, not a floor."""
    sizer = _make_sizer(strategy_caps={"mean_reversion": 200})
    qty = sizer.calculate(
        balance=10_000, price=20.0, stop_loss=19.0,
        strategy="mean_reversion",
    )
    # Risk_dollars 100 < cap 200; cap doesn't bite.
    assert qty == 75  # same as uncapped


def test_capped_strategy_above_cap_shrinks():
    """When raw risk_dollars exceeds the cap, risk and shares both shrink."""
    sizer = _make_sizer(strategy_caps={"mean_reversion": 50})
    qty = sizer.calculate(
        balance=10_000, price=20.0, stop_loss=19.0,
        strategy="mean_reversion",
    )
    # Without cap: 100, capped at 75. With $50 cap: risk = 50,
    # shares_by_risk = 50/1 = 50. shares_by_max still 75.
    # Final: min(50, 75) = 50.
    assert qty == 50


def test_near_usd_class_scenario_bounded():
    """NEAR-USD 2026-06-04: balance ~24K, price 2.65, stop 2.52 (5%).
    Without cap: risk ~$240, position ~$2,870. With $50 cap: position
    shrinks to ~$1,000, max loss bounded to $50."""
    sizer = _make_sizer(strategy_caps={"mean_reversion": 50})
    qty = sizer.calculate(
        balance=24_000, price=2.65, stop_loss=2.5165,
        strategy="mean_reversion",
        symbol="NEAR-USD",  # crypto path uses fractional sizing
    )
    # per_share_risk = 0.1335. risk_dollars capped at 50.
    # qty = 50 / 0.1335 = 374.5 (rounded to 5 decimals on crypto)
    assert qty > 0
    # Max loss bounded: qty × per_share_risk ≈ $50
    max_loss = qty * (2.65 - 2.5165)
    assert max_loss <= 50.5, f"Max loss ${max_loss:.2f} should be ≤ cap $50"
    assert max_loss > 49.0, f"Max loss ${max_loss:.2f} should be near cap"


def test_cap_does_not_affect_other_strategies():
    """mean_reversion capped at $50 must not change momentum sizing."""
    sizer = _make_sizer(strategy_caps={"mean_reversion": 50})
    qty_mr = sizer.calculate(
        balance=10_000, price=20.0, stop_loss=19.0,
        strategy="mean_reversion",
    )
    qty_mom = sizer.calculate(
        balance=10_000, price=20.0, stop_loss=19.0,
        strategy="momentum",
    )
    assert qty_mom > qty_mr, (
        f"Momentum ({qty_mom}) must size larger than capped mean_reversion "
        f"({qty_mr}) with identical inputs."
    )
    assert qty_mom == 75  # full size for uncapped strategy


def test_cap_value_zero_disables_strategy_cap():
    """Setting cap to 0 means "no cap" — falsy values disable the rule."""
    sizer = _make_sizer(strategy_caps={"mean_reversion": 0})
    qty = sizer.calculate(
        balance=10_000, price=20.0, stop_loss=19.0,
        strategy="mean_reversion",
    )
    assert qty == 75  # behaves as uncapped


def test_cap_applies_to_crypto_fractional_sizing():
    """Crypto path uses fractional qty (qty * price >= $10 dust). Cap must
    still bite, but min position floor must hold (don't size to $9.99)."""
    sizer = _make_sizer(strategy_caps={"mean_reversion": 50})
    qty = sizer.calculate(
        balance=50_000, price=0.50, stop_loss=0.475,  # 5% stop on cheap crypto
        strategy="mean_reversion",
        symbol="BONK-USD",
    )
    # per_share_risk = 0.025. risk capped at 50. qty = 50/0.025 = 2000.
    # qty × price = $1000 > $10 dust threshold.
    assert qty > 0
    max_loss = qty * 0.025
    assert max_loss <= 50.5
