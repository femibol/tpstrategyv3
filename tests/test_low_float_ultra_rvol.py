"""low_float_catalyst applies a relaxed RVOL gate for ultra-low-float names.

Live case 2026-05-28: MASK alerted at $1.72 with **820K float** and
**RVOL 1.00x**, then ran to $6.73 (+199%) within hours. Our default
`min_rvol = 5x` would have rejected MASK at the alert window (1.00 < 5)
even though every other gate (float ≤75M, change ≥15%, price ≥$0.20)
cleared easily.

The 5x RVOL screen is calibrated for normal "low float" names (≤75M
shares). On ultra-low floats — say 820K — even modest volume produces
dramatic price moves; relative volume is a noisier metric there because
the comparison baseline is tiny. The MASK-class setup needs a different
threshold.

Fix: float-adjusted RVOL threshold.
  - Float ≤ `ultra_low_float_threshold_m` (default 2M shares): RVOL ≥
    `ultra_low_float_min_rvol` (default 2.0x) admits the signal
  - Float > 2M but ≤ 75M (normal low-float): RVOL ≥ 5.0x still required

First-pass screen uses the LOOSER threshold across both regimes (2x) so
names below that get rejected before the expensive Finviz call. Strict
threshold applied AFTER float is known.

These tests pin five guarantees:
  1. Ultra-low-float (≤2M) + RVOL ≥2x = signal fires
  2. Ultra-low-float (≤2M) + RVOL < 2x = still rejected
  3. Normal float (>2M) + RVOL between 2x and 5x = rejected (gate still strict)
  4. Normal float (>2M) + RVOL ≥5x = signal fires (unchanged behavior)
  5. Unknown float (Finviz failure path) = strict threshold (don't be
     over-permissive on missing data)
"""
from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest


def _strat():
    from bot.strategies.low_float_catalyst import LowFloatCatalystStrategy

    cfg = {
        "min_price": 0.20, "max_price": 10.00, "max_float_m": 75.0,
        "min_rvol": 5.0, "max_spread_pct": 0.03,
        "min_day_change_pct": 15.0, "hard_stop_pct": 0.08,
        "hard_target_pct": 0.20, "max_hold_minutes": 30,
        "max_trades_per_day": 8, "open_dead_zone_start_min": 25,
        "open_dead_zone_end_min": 35, "require_known_float": False,
        "max_position_pct": 0.05,
        "ultra_low_float_threshold_m": 2.0,
        "ultra_low_float_min_rvol": 2.0,
    }
    return LowFloatCatalystStrategy(cfg, indicators=None, capital=10000)


class _FakeMarketData:
    def __init__(self, quote, bars):
        self._quote = quote
        self._bars = bars
    def get_quote(self, symbol):
        return self._quote
    def get_bars(self, symbol, n):
        return self._bars


def _make_bars(rvol_multiplier):
    avg_vol = 100_000
    vols = [avg_vol] * 19 + [int(avg_vol * rvol_multiplier)]
    closes = [1.0] * 20
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": vols,
    })


def _try_signal(strat, symbol, change_pct, rvol_mult, float_m):
    strat._dynamic_symbols.add(symbol)
    quote = {"price": 1.46, "change_pct": change_pct, "bid": 1.45, "ask": 1.46}
    bars = _make_bars(rvol_mult)
    md = _FakeMarketData(quote, bars)
    with mock.patch("bot.strategies.low_float_catalyst.get_float", return_value=float_m):
        sigs = strat.generate_signals(md)
    return sigs


# === Ultra-low-float regime (≤ 2M shares) — RELAXED 2x threshold ===


def test_ultra_low_float_with_rvol_2x_fires():
    """MASK-class: 820K float, RVOL 2.0x, change +24%. Should pass."""
    sigs = _try_signal(_strat(), "MASK", change_pct=24.0, rvol_mult=2.0,
                        float_m=0.820)  # 820K = 0.82M
    assert len(sigs) == 1
    assert sigs[0]["score"] >= 50


def test_ultra_low_float_with_rvol_below_2x_rejected():
    """Even an ultra-tiny float needs some volume signal."""
    sigs = _try_signal(_strat(), "MASK", change_pct=24.0, rvol_mult=1.5,
                        float_m=0.820)
    assert len(sigs) == 0


def test_ultra_low_float_with_rvol_2x_change_at_minimum_fires():
    """820K float, RVOL 2.0x, day_change exactly at the 15% threshold."""
    sigs = _try_signal(_strat(), "MASK", change_pct=15.5, rvol_mult=2.0,
                        float_m=0.820)
    assert len(sigs) == 1


# === Normal float regime (>2M but ≤75M) — STRICT 5x still required ===


def test_normal_float_with_rvol_3x_rejected():
    """20M float, RVOL 3x — relaxed threshold doesn't apply, strict 5x does."""
    sigs = _try_signal(_strat(), "VRAX", change_pct=20.0, rvol_mult=3.0,
                        float_m=20.0)
    assert len(sigs) == 0


def test_normal_float_with_rvol_5x_fires():
    """20M float, RVOL 5x — unchanged behavior, signal fires."""
    sigs = _try_signal(_strat(), "VRAX", change_pct=20.0, rvol_mult=5.0,
                        float_m=20.0)
    assert len(sigs) == 1


def test_normal_float_with_rvol_4x_below_strict_threshold_rejected():
    """4x RVOL — above 2x relaxed BUT below 5x strict. Normal float gets strict."""
    sigs = _try_signal(_strat(), "VRAX", change_pct=20.0, rvol_mult=4.0,
                        float_m=20.0)
    assert len(sigs) == 0


# === Boundary: exactly at 2M float ===


def test_float_exactly_at_threshold_uses_relaxed():
    """Float == ultra_low_float_threshold_m: relaxed threshold applies
    (inclusive boundary)."""
    sigs = _try_signal(_strat(), "EDGE", change_pct=20.0, rvol_mult=2.0,
                        float_m=2.0)
    assert len(sigs) == 1


def test_float_just_above_threshold_uses_strict():
    """Float > threshold: strict threshold applies. RVOL 2.0x fails."""
    sigs = _try_signal(_strat(), "JUSTOVER", change_pct=20.0, rvol_mult=2.0,
                        float_m=2.5)
    assert len(sigs) == 0


# === Unknown float (Finviz failure) → defaults to strict ===


def test_unknown_float_with_relaxed_rvol_rejected():
    """When float is None and require_known_float=False, the strict 5x
    threshold applies. Don't go permissive on missing data."""
    sigs = _try_signal(_strat(), "UNKNOWN", change_pct=20.0, rvol_mult=2.5,
                        float_m=None)
    assert len(sigs) == 0


def test_unknown_float_with_strict_rvol_fires():
    sigs = _try_signal(_strat(), "UNKNOWN", change_pct=20.0, rvol_mult=5.5,
                        float_m=None)
    assert len(sigs) == 1


# === First-pass RVOL screen: rejects below the LOOSEST threshold without
#     spending a Finviz call ===


def test_first_pass_rejects_below_loose_threshold_no_finviz():
    """RVOL well below the loose 2.0x floor — Finviz must NOT be called
    (would waste a network round-trip on a clear-cut reject)."""
    strat = _strat()
    strat._dynamic_symbols.add("LOWVOL")
    quote = {"price": 1.46, "change_pct": 20.0, "bid": 1.45, "ask": 1.46}
    bars = _make_bars(rvol_multiplier=1.0)
    with mock.patch("bot.strategies.low_float_catalyst.get_float") as mock_float:
        sigs = strat.generate_signals(_FakeMarketData(quote, bars))
        # The float lookup must not have happened — we rejected pre-Finviz.
        assert mock_float.call_count == 0
    assert len(sigs) == 0
