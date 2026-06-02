"""rvol_scalp signals must carry a `score` field clearing min_entry_score.

Live incident 2026-06-02: NU and IBIT fired SCALP signals every 3 minutes
all session — internal score 85/90, RVOL 3.9–4.6x, R:R 2.0. Engine logged
`QUALITY GATE SKIP: NU — score 0 below min 50` and the matching skip for
IBIT, in lockstep, for hours. Zero rvol_scalp fills.

Same shape as the SNBR / low_float_catalyst bug fixed in PR #189: the
strategy computes `score` internally (lines 199–243 of rvol_scalp.py)
and uses it in scan_result + to derive `confidence`, but never copies
it into the signal dict returned to the engine. The QUALITY GATE at
engine.py:7990 reads `signal.get("score", 0)`, sees 0, and kills the
entry post-approval.

Fix: pass the strategy's own `score` directly into the signal dict.
It's already bounded 0–100 by the additive scoring system, and the
strategy's own `verdict == "SCALP SIGNAL"` gate requires score >=
effective_min_score (60 equity / 48 crypto), so by construction any
fired signal already clears the engine's 50.

These tests pin three invariants:
  1. The shipped signal dict includes the `score` key
  2. The score is >= 50 (engine's min_entry_score default)
  3. The score is a clean int in 0–100 (engine compares numerically)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _strat():
    from bot.data.indicators import TechnicalIndicators
    from bot.strategies.rvol_scalp import RvolScalpStrategy

    cfg = {
        "min_rvol": 2.5, "min_score": 60, "min_price": 2.00,
        "max_price": 50.00, "min_volume": 200_000,
        "atr_stop_multiplier": 1.0, "atr_target_multiplier": 2.0,
        "quick_scalp_target_pct": 0.012, "runner_target_pct": 0.025,
        "max_hold_minutes": 15, "max_trades_per_day": 15,
        "breakout_confirmation_bars": 2, "momentum_acceleration": True,
    }
    return RvolScalpStrategy(cfg, TechnicalIndicators(), capital=10000)


class _FakeMarketData:
    def __init__(self, bars):
        self._bars = bars

    def get_quote(self, symbol):
        return {"price": float(self._bars["close"].iloc[-1])}

    def get_bars(self, symbol, n, bar_size=None):
        return self._bars


def _make_scalp_bars():
    """30 1-min bars with a strong RVOL breakout in the final bars.
    Tuned to produce: RVOL ~4x, confirmed breakout, vol accelerating,
    bullish trend → score easily >= 60 for an equity SCALP SIGNAL."""
    n = 30
    # 28 bars of base price 10.00 with normal volume (300k/bar avg)
    base_price = 10.0
    closes = [base_price] * (n - 2)
    opens = [base_price] * (n - 2)
    highs = [base_price + 0.02] * (n - 2)
    lows = [base_price - 0.02] * (n - 2)
    vols = [300_000] * (n - 2)

    # Last 2 bars: strong up-bars with rising volume (breakout + vol accel)
    # Bar -2: open 10.00, close 10.10 (+1%), vol 800k
    # Bar -1: open 10.10, close 10.25 (+1.5%), vol 1.2M (RVOL ~4x avg)
    opens.append(10.00); closes.append(10.10)
    highs.append(10.11); lows.append(9.99)
    vols.append(800_000)

    opens.append(10.10); closes.append(10.25)
    highs.append(10.26); lows.append(10.09)
    vols.append(1_200_000)

    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    })


def test_scalp_signal_dict_contains_score_field():
    """A fired SCALP signal MUST include `score`. Without it the engine's
    QUALITY GATE defaults to 0 and skips the entry. See module docstring
    for the NU/IBIT 2026-06-02 live bleed."""
    strat = _strat()
    strat._dynamic_symbols.add("FOO")
    md = _FakeMarketData(_make_scalp_bars())
    sigs = strat.generate_signals(md)
    assert len(sigs) == 1, (
        f"Test fixture must produce exactly 1 SCALP signal; got {len(sigs)}. "
        f"If you've tightened the strategy's gates, retune the bars."
    )
    assert "score" in sigs[0], (
        "rvol_scalp signal dict MUST include 'score' — QUALITY GATE at "
        "engine.py:7990 defaults missing scores to 0 and kills the entry."
    )


def test_score_clears_min_entry_score_default():
    """Engine default min_entry_score = 50. By construction, a fired
    rvol_scalp signal has internal score >= effective_min_score (60 for
    equity), so it always clears 50."""
    strat = _strat()
    strat._dynamic_symbols.add("FOO")
    sigs = strat.generate_signals(_FakeMarketData(_make_scalp_bars()))
    assert sigs[0]["score"] >= 50, (
        f"score must be >= 50 (engine min_entry_score). Got {sigs[0]['score']}."
    )


def test_score_is_int_in_engine_consumable_range():
    """Engine compares `score < min_entry_score` numerically. Must be a
    real int, not a string, dict, or None."""
    strat = _strat()
    strat._dynamic_symbols.add("FOO")
    sig = strat.generate_signals(_FakeMarketData(_make_scalp_bars()))[0]
    assert isinstance(sig["score"], int)
    assert 0 <= sig["score"] <= 100
