"""QUALITY GATE spread rule: tiered by price + auto size-down for runners.

The flat 2% spread rule was tuned for mid-cap equity. It blocked exactly
the micro-cap runners we want to catch — LGPS ($0.79, 17.6x RVOL) and
TSAT (sub-$2) were SIGNAL → QUALITY GATE SKIP for HOURS on 2026-06-04,
each emitting "wide spread 3-8% (>2%)" every 3 minutes while they ran.

New behavior (in priority order — first match wins):
  - $5+        → 2% strict (current behavior on liquid mid-cap)
  - $2 to $5   → 3% allowed
  - sub-$2     → 5% allowed

When spread is above the strict 2% baseline but within the price-tier
ceiling, size_multiplier is reduced linearly from 1.0 (at 2%) to 0.5
(at the tier ceiling). So a $0.79 LGPS at 3.9% spread gets ~0.69x sizing
— smaller position to bound the slippage cost.

Config override: `risk.max_spread_pct_tiers: {lt_2: 0.05, lt_5: 0.03,
default: 0.02}`.

These tests pin five guarantees:
  1. Mid-cap ($5+) still rejected at >2% spread (no regression)
  2. Small-cap ($2-$5) allowed up to 3% spread
  3. Micro-cap (sub-$2) allowed up to 5% spread
  4. Wide-tier spread triggers size_multiplier reduction toward 0.5
  5. Existing size_multiplier (e.g. from runner sizing) compounds with
     the spread size-down
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_engine_with_book(spread_pct, imbalance=0.0):
    """Build a stub engine with a mocked broker order-book."""
    from bot.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine.trade_history = []
    engine.positions = {}
    engine.config = SimpleNamespace(risk_config={})
    engine.current_regime = "neutral"

    # Mock broker that always returns the requested spread + imbalance
    broker = MagicMock()
    broker.is_connected.return_value = True
    broker.get_order_book.return_value = {
        "spread_pct": spread_pct,
        "imbalance": imbalance,
    }
    engine.broker = broker
    return engine


# === 1. Mid-cap regression ===


def test_midcap_rejected_above_strict_2pct():
    """$25 stock at 3% spread: must still reject (>= 2%, $5+ tier)."""
    engine = _make_engine_with_book(spread_pct=0.03)
    sig = {"symbol": "AMD", "action": "buy", "price": 25.0,
           "strategy": "momentum", "score": 80, "rvol": 3.0}
    passed, reason = engine._entry_quality_gate(sig)
    assert not passed
    assert "wide spread" in reason
    assert "2%" in reason


def test_midcap_passes_at_strict_2pct():
    """$25 stock at 1.5% spread: must pass."""
    engine = _make_engine_with_book(spread_pct=0.015)
    sig = {"symbol": "AMD", "action": "buy", "price": 25.0,
           "strategy": "momentum", "score": 80, "rvol": 3.0}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed


# === 2-3. Tiered allowance ===


def test_smallcap_allowed_at_3pct_spread():
    """$3 stock at 2.8% spread: in $2-$5 tier (max 3%) → passes."""
    engine = _make_engine_with_book(spread_pct=0.028)
    sig = {"symbol": "SOFI", "action": "buy", "price": 3.0,
           "strategy": "momentum", "score": 80, "rvol": 3.0}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed


def test_smallcap_rejected_above_tier_3pct():
    """$3 stock at 4% spread: above $2-$5 tier (3%) → rejected."""
    engine = _make_engine_with_book(spread_pct=0.04)
    sig = {"symbol": "SOFI", "action": "buy", "price": 3.0,
           "strategy": "momentum", "score": 80, "rvol": 3.0}
    passed, reason = engine._entry_quality_gate(sig)
    assert not passed
    assert "wide spread" in reason


def test_microcap_lgps_pattern_now_passes():
    """LGPS-style: $0.79, 3.9% spread. Previously blocked, now must pass."""
    engine = _make_engine_with_book(spread_pct=0.039)
    sig = {"symbol": "LGPS", "action": "buy", "price": 0.79,
           "strategy": "momentum_runner", "score": 80, "rvol": 17.6}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed, (
        "Sub-$2 stock at 3.9% spread MUST pass — this is exactly the "
        "MASK/LGPS class we wrote low_float_catalyst to catch."
    )


def test_microcap_tsat_at_5pct_passes():
    """TSAT-style: sub-$2 at exactly 5% spread (the tier ceiling)."""
    engine = _make_engine_with_book(spread_pct=0.05)
    sig = {"symbol": "TSAT", "action": "buy", "price": 1.50,
           "strategy": "low_float_catalyst", "score": 80, "rvol": 3.0}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed


def test_microcap_above_5pct_still_rejected():
    """Sub-$2 stock at 8% spread: above tier ceiling → rejected.
    Even on micro-caps, 8% spread is too punishing — eats most of the
    move on a winning trade."""
    engine = _make_engine_with_book(spread_pct=0.08)
    sig = {"symbol": "TSAT", "action": "buy", "price": 1.50,
           "strategy": "low_float_catalyst", "score": 80, "rvol": 3.0}
    passed, reason = engine._entry_quality_gate(sig)
    assert not passed
    assert "wide spread" in reason


# === 4. Size-down on moderate spread ===


def test_size_multiplier_set_when_spread_above_strict():
    """LGPS at 3.9% spread: passes but with size_multiplier reduced."""
    engine = _make_engine_with_book(spread_pct=0.039)
    sig = {"symbol": "LGPS", "action": "buy", "price": 0.79,
           "strategy": "momentum_runner", "score": 80, "rvol": 17.6}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed
    # Spread 3.9% in sub-$2 tier (max 5%): scale should be in (0.5, 1.0)
    # over = 0.039 - 0.020 = 0.019; rng = 0.05 - 0.02 = 0.03
    # scale = 1.0 - (0.019/0.03)*0.5 = 1.0 - 0.317 = 0.683
    assert 0.6 < sig.get("size_multiplier", 1.0) < 0.75


def test_size_multiplier_at_tier_ceiling_is_half():
    """At the tier ceiling (5% on micro-cap), multiplier hits 0.5 floor."""
    engine = _make_engine_with_book(spread_pct=0.05)
    sig = {"symbol": "TSAT", "action": "buy", "price": 1.50,
           "strategy": "low_float_catalyst", "score": 80, "rvol": 3.0}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed
    assert sig.get("size_multiplier", 1.0) == 0.5


def test_size_multiplier_unchanged_when_spread_at_or_below_strict():
    """At or below 2%, the existing 1.0 multiplier is preserved (no
    over-shrinking on already-tight names)."""
    engine = _make_engine_with_book(spread_pct=0.015)
    sig = {"symbol": "LGPS", "action": "buy", "price": 0.79,
           "strategy": "momentum_runner", "score": 80, "rvol": 17.6}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed
    # No size_multiplier should be set (default 1.0 applies downstream)
    assert sig.get("size_multiplier", 1.0) == 1.0


# === 5. Compounding with existing multiplier ===


def test_existing_size_multiplier_compounds():
    """If a strategy already set size_multiplier=0.5 (e.g. runner spike
    entry), the spread size-down multiplies onto that — never overwrites."""
    engine = _make_engine_with_book(spread_pct=0.05)
    sig = {"symbol": "TSAT", "action": "buy", "price": 1.50,
           "strategy": "low_float_catalyst", "score": 80, "rvol": 3.0,
           "size_multiplier": 0.5}  # already 50% from upstream
    passed, _ = engine._entry_quality_gate(sig)
    assert passed
    # 0.5 (upstream) × 0.5 (spread floor) = 0.25
    assert sig.get("size_multiplier", 1.0) == 0.25


# === 6. Config override ===


def test_spread_tiers_overrideable_via_config():
    """Config can tighten the rules (e.g. aggressive mode wanting strict 2%)."""
    engine = _make_engine_with_book(spread_pct=0.035)
    engine.config.risk_config["max_spread_pct_tiers"] = {
        "lt_2": 0.02, "lt_5": 0.02, "default": 0.02
    }
    # Sub-$2 stock at 3.5%: with tightened tiers all = 2%, rejected
    sig = {"symbol": "LGPS", "action": "buy", "price": 0.79,
           "strategy": "momentum_runner", "score": 80, "rvol": 17.6}
    passed, _ = engine._entry_quality_gate(sig)
    assert not passed
