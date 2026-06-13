"""Slippage-aware risk caps (Wave 2 audit fix).

The `slippage_reject` hard threshold (max_slippage_pct = 0.8% RTH /
1.5% extended) handles the CATASTROPHIC case: HIBS-style 5-second
blowups where the fill came in unusably above signal. But the
strategy review on momentum / rvol_scalp / low_float_catalyst
showed a different leak: fills that pass slippage_reject (worst
0.5-0.7%) still eat the strategy's edge over time. 10 trades at
0.5% average adverse slippage on a $250 average size = $12.50 of
pure friction per trade, > 50% of the typical edge.

Fix: track per-strategy realized adverse slippage in a rolling deque
and dampen the next entry's size when recent average exceeds 0.3%.
Parallel shape to the existing `vol_regime_mult` — protective only,
floor at 0.5×.

These tests pin:
  1. Position sizer accepts `slippage_mult` and applies it
  2. Sizer clamps mult to [0.5, 1.0] (protective only)
  3. Engine helper `_compute_slippage_mult` curve
  4. Insufficient sample (< 5 fills) → 1.0 (don't preemptively dampen)
  5. Adverse-only recording (discounts don't count)
"""
from __future__ import annotations

from collections import deque, defaultdict
from types import SimpleNamespace

import pytest


# === 1. Position sizer integration ===


def _make_sizer():
    from bot.risk.position_sizer import PositionSizer
    cfg = SimpleNamespace(
        settings={
            "risk": {
                "risk_per_trade_pct": 0.01,
                "max_position_pct": 0.20,
                "max_position_size_pct": 0.20,
                "reserve_cash_pct": 0.05,
            },
            "capital": {"starting_balance": 10000},
            "crypto": {},
        },
        risk_config={
            "risk_per_trade_pct": 0.01,
            "max_position_pct": 0.20,
            "max_position_size_pct": 0.20,
            "reserve_cash_pct": 0.05,
        },
        risk_per_trade=0.01,
        starting_balance=10000,
        reserve_cash_pct=0.05,
        is_paper=True,
        is_live=False,
    )
    return PositionSizer(cfg)


def test_sizer_accepts_slippage_mult_kwarg():
    """Smoke: the kwarg exists and a 1.0 value produces baseline size."""
    sizer = _make_sizer()
    qty = sizer.calculate(
        balance=10000, price=100.0, stop_loss=95.0,
        strategy_allocation=1.0, slippage_mult=1.0, strategy="test",
    )
    assert qty > 0  # baseline


def test_sizer_dampens_at_0_5_mult():
    """0.5× should halve the share count vs 1.0×."""
    sizer = _make_sizer()
    qty_full = sizer.calculate(
        balance=10000, price=100.0, stop_loss=95.0,
        strategy_allocation=1.0, slippage_mult=1.0, strategy="test",
    )
    qty_half = sizer.calculate(
        balance=10000, price=100.0, stop_loss=95.0,
        strategy_allocation=1.0, slippage_mult=0.5, strategy="test",
    )
    assert qty_half < qty_full
    # Allow some tolerance — other multipliers + caps may interact
    assert qty_half >= int(qty_full * 0.4)  # not less than 0.4x (slip floor at 0.5x)
    assert qty_half <= int(qty_full * 0.6)  # not more than 0.6x


def test_sizer_clamps_slippage_mult_to_protective_only():
    """A slippage_mult > 1.0 must NOT size UP — clamped to 1.0 ceiling.
    The dampener is one-way protective by design."""
    sizer = _make_sizer()
    qty_normal = sizer.calculate(
        balance=10000, price=100.0, stop_loss=95.0,
        strategy_allocation=1.0, slippage_mult=1.0, strategy="test",
    )
    qty_inflated = sizer.calculate(
        balance=10000, price=100.0, stop_loss=95.0,
        strategy_allocation=1.0, slippage_mult=2.0, strategy="test",  # silly value
    )
    assert qty_inflated == qty_normal  # clamped to 1.0


def test_sizer_clamps_below_floor():
    """slippage_mult below 0.5 is clamped UP to 0.5 — don't size to zero."""
    sizer = _make_sizer()
    qty_floor = sizer.calculate(
        balance=10000, price=100.0, stop_loss=95.0,
        strategy_allocation=1.0, slippage_mult=0.5, strategy="test",
    )
    qty_below = sizer.calculate(
        balance=10000, price=100.0, stop_loss=95.0,
        strategy_allocation=1.0, slippage_mult=0.1, strategy="test",  # below floor
    )
    assert qty_below == qty_floor  # clamped


# === 2. Engine helper curve ===


def _engine_with_slippage_buf(strategy, samples):
    """Build a minimal engine stub with the slippage tracking buffer
    populated. Doesn't instantiate the full TradingEngine — just the
    methods under test."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    eng._strategy_slippage = defaultdict(lambda: deque(maxlen=20))
    eng._strategy_slippage[strategy].extend(samples)
    return eng


def test_compute_slippage_mult_empty_buffer_returns_1():
    """Day 0 of a strategy: no recorded fills → no dampening."""
    eng = _engine_with_slippage_buf("momentum", [])
    assert eng._compute_slippage_mult("momentum") == 1.0


def test_compute_slippage_mult_insufficient_sample_returns_1():
    """4 fills isn't enough to make a sizing decision."""
    eng = _engine_with_slippage_buf("momentum", [0.01, 0.01, 0.01, 0.01])
    assert eng._compute_slippage_mult("momentum") == 1.0


def test_compute_slippage_mult_low_avg_returns_1():
    """5+ fills but avg < 0.3% → no dampening (clean fills)."""
    eng = _engine_with_slippage_buf(
        "momentum", [0.001, 0.002, 0.001, 0.002, 0.001, 0.001],
    )
    assert eng._compute_slippage_mult("momentum") == 1.0


def test_compute_slippage_mult_mid_avg_dampens():
    """avg around 0.45% → mult around 0.75."""
    eng = _engine_with_slippage_buf(
        "momentum", [0.005, 0.004, 0.005, 0.004, 0.005, 0.004],
    )
    mult = eng._compute_slippage_mult("momentum")
    assert 0.5 <= mult <= 1.0
    assert mult < 1.0
    assert mult > 0.5


def test_compute_slippage_mult_high_avg_hits_floor():
    """avg >= 0.6% → 0.5 floor."""
    eng = _engine_with_slippage_buf(
        "momentum", [0.008, 0.009, 0.007, 0.008, 0.010, 0.008],
    )
    mult = eng._compute_slippage_mult("momentum")
    assert mult == 0.5


def test_compute_slippage_mult_scoped_per_strategy():
    """momentum's bad slippage doesn't dampen mean_reversion sizing."""
    eng = _engine_with_slippage_buf(
        "momentum", [0.008, 0.008, 0.008, 0.008, 0.008, 0.008],
    )
    eng._strategy_slippage["mean_reversion"].extend(
        [0.001, 0.001, 0.001, 0.001, 0.001, 0.001]
    )
    assert eng._compute_slippage_mult("momentum") == 0.5
    assert eng._compute_slippage_mult("mean_reversion") == 1.0


# === 3. Adverse-only recording ===


def test_record_slippage_treats_negative_as_zero():
    """A favorable fill (negative slippage = discount) isn't friction.
    Recording it as adverse would mask actual drag."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    eng._record_slippage("test", -0.005)  # filled BELOW signal
    eng._record_slippage("test", -0.003)
    eng._record_slippage("test", -0.001)
    eng._record_slippage("test", 0.0)
    eng._record_slippage("test", 0.001)
    eng._record_slippage("test", 0.002)
    # Average of recorded adverse values: (0+0+0+0+0.001+0.002)/6 = 0.0005
    mult = eng._compute_slippage_mult("test")
    assert mult == 1.0  # well under 0.3% threshold


def test_record_slippage_handles_missing_strategy():
    """A signal with strategy=None must not crash the recorder."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    eng._record_slippage(None, 0.005)  # no-op
    eng._record_slippage("", 0.005)  # no-op
    # No crash; no tracking happened
    assert not hasattr(eng, "_strategy_slippage") or \
        all(len(v) == 0 for v in eng._strategy_slippage.values())


def test_buffer_capped_at_20_entries():
    """Rolling deque drops oldest — recent slippage dominates the avg.
    A strategy that fixed its execution shouldn't be penalized forever
    by an old streak."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    # 30 high-slippage fills, then 20 clean — the clean ones evict
    for _ in range(30):
        eng._record_slippage("strat", 0.010)  # high
    for _ in range(20):
        eng._record_slippage("strat", 0.001)  # clean
    mult = eng._compute_slippage_mult("strat")
    assert mult == 1.0  # only the clean window is "remembered"
