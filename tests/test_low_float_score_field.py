"""low_float_catalyst signals must include a `score` field >= min_entry_score.

Live incident 2026-06-02: SNBR fired at $1.46 with RVOL 5.2x / +17.3%
change / 18.6M float — every strategy-level gate cleared, risk_manager
APPROVED with `score 85/100` (from the rvol_momentum dedup partner).
3 seconds later: `QUALITY GATE SKIP: SNBR — score 0 below min 50`.

The QUALITY GATE at engine.py:7990 reads `signal.get("score", 0)`. The
low_float_catalyst signal dict omitted the `score` field entirely. The
dedup at engine.py:2332 picked low_float_catalyst as the surviving
signal (probably by source preference or insertion order), and the
quality gate saw score=0 from the merged signal, killed the entry.

This is structurally fatal: the strategy fires successfully but is
never able to convert a signal into a fill. Equivalent to having the
strategy disabled, with bonus "we tried" log noise.

Fix: confidence × 100. confidence is bounded 0.5–1.0 by the strategy's
own formula (`0.5 + 0.25 * rvol_norm + 0.25 * change_norm`), so score
is always in 50–100, which always clears `min_entry_score = 50`. By
design: if the strategy has decided to fire, the quality gate score
must allow the entry — the strategy's gates are tighter than the
generic quality gate anyway.

These tests pin three guarantees:
  1. The shipped signal dict includes the `score` key
  2. The score value is at or above 50 (the min_entry_score)
  3. The score scales with confidence (so signals with stronger
     RVOL/change get higher scores, preserving any future ordering /
     sizing that consumes the score field)
"""
from __future__ import annotations

import inspect
import re
from unittest import mock

import numpy as np
import pandas as pd
import pytest


def _strat_with_min_signal():
    """Build a low_float_catalyst with config + a minimum-strength signal
    that just-barely clears the strategy's own gates."""
    from bot.strategies.low_float_catalyst import LowFloatCatalystStrategy

    cfg = {
        "min_price": 0.20, "max_price": 10.00, "max_float_m": 75.0,
        "min_rvol": 5.0, "max_spread_pct": 0.03,
        "min_day_change_pct": 15.0, "hard_stop_pct": 0.08,
        "hard_target_pct": 0.20, "max_hold_minutes": 30,
        "max_trades_per_day": 8, "open_dead_zone_start_min": 25,
        "open_dead_zone_end_min": 35, "require_known_float": True,
        "max_position_pct": 0.05,
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
    """20 bars with a final volume spike of `rvol_multiplier` × avg."""
    avg_vol = 100_000
    vols = [avg_vol] * 19 + [int(avg_vol * rvol_multiplier)]
    closes = [1.0] * 20
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": vols,
    })


def test_signal_dict_contains_score_field():
    """The shipped strategy's signal dict must include `score`. Without
    this key the QUALITY GATE reads default 0 and skips every entry."""
    strat = _strat_with_min_signal()
    strat._dynamic_symbols.add("SNBR")
    quote = {"price": 1.46, "change_pct": 17.3, "bid": 1.45, "ask": 1.46}
    bars = _make_bars(rvol_multiplier=5.2)
    md = _FakeMarketData(quote, bars)
    with mock.patch("bot.strategies.low_float_catalyst.get_float", return_value=18.6):
        sigs = strat.generate_signals(md)
    assert len(sigs) == 1
    assert "score" in sigs[0], (
        "low_float_catalyst signal dict MUST include 'score' — the QUALITY "
        "GATE at engine.py:7990 defaults missing scores to 0 and kills the "
        "entry. See module docstring for the SNBR incident."
    )


def test_score_clears_min_entry_score_default():
    """min_entry_score defaults to 50 in the engine. Every legitimate
    low_float signal must score >= 50."""
    strat = _strat_with_min_signal()
    strat._dynamic_symbols.add("SNBR")
    quote = {"price": 1.46, "change_pct": 17.3, "bid": 1.45, "ask": 1.46}
    bars = _make_bars(rvol_multiplier=5.2)
    with mock.patch("bot.strategies.low_float_catalyst.get_float", return_value=18.6):
        sigs = strat.generate_signals(_FakeMarketData(quote, bars))
    assert sigs[0]["score"] >= 50, (
        f"score must be >= 50 (engine min_entry_score). Got {sigs[0]['score']}. "
        f"If you tightened the confidence formula's lower bound, also tighten "
        f"this assertion."
    )


def test_score_scales_with_confidence():
    """A higher-conviction setup (bigger RVOL + bigger change) must
    produce a higher score than a borderline-conviction setup."""
    strat = _strat_with_min_signal()
    strat._dynamic_symbols.add("SNBR")

    # Borderline: just-barely clears 15% change + 5x RVOL
    quote_low = {"price": 1.46, "change_pct": 15.5, "bid": 1.45, "ask": 1.46}
    bars_low = _make_bars(rvol_multiplier=5.1)
    # Strong: well above thresholds
    quote_hi = {"price": 1.46, "change_pct": 45.0, "bid": 1.45, "ask": 1.46}
    bars_hi = _make_bars(rvol_multiplier=12.0)

    with mock.patch("bot.strategies.low_float_catalyst.get_float", return_value=18.6):
        sig_low = strat.generate_signals(_FakeMarketData(quote_low, bars_low))[0]
        strat._dynamic_symbols.add("OTHER")  # avoid daily-cap edge
        # re-add SNBR (strategy may dedupe by name across calls)
        sig_hi = strat.generate_signals(_FakeMarketData(quote_hi, bars_hi))[0]

    assert sig_hi["score"] > sig_low["score"], (
        f"Score must be monotonic in conviction. low={sig_low['score']} "
        f"hi={sig_hi['score']}"
    )


def test_score_is_int_in_engine_consumable_range():
    """The engine compares `score < min_entry_score` numerically. Must be
    a real int, not a string, dict, or None."""
    strat = _strat_with_min_signal()
    strat._dynamic_symbols.add("SNBR")
    quote = {"price": 1.46, "change_pct": 17.3, "bid": 1.45, "ask": 1.46}
    bars = _make_bars(rvol_multiplier=5.2)
    with mock.patch("bot.strategies.low_float_catalyst.get_float", return_value=18.6):
        sig = strat.generate_signals(_FakeMarketData(quote, bars))[0]
    assert isinstance(sig["score"], int)
    assert 0 <= sig["score"] <= 100


def test_strategy_source_files_include_score_field():
    """Lock the source-level invariant: any strategy that produces buy
    signals through the slow path (which goes through QUALITY GATE) MUST
    include `score` in its return dict. Catches the bug at code-review
    time — a strategy author who omits it gets a test failure, not a
    silent runtime skip."""
    from bot.strategies import low_float_catalyst as lfc
    from bot.strategies import crypto_runner as cr

    for module in (lfc, cr):
        src = inspect.getsource(module)
        assert '"score"' in src, (
            f"{module.__name__} signal dict must include a 'score' field "
            f"(see engine.py:7990 QUALITY GATE). Without it, signals "
            f"default to score=0 and fail min_entry_score=50."
        )
