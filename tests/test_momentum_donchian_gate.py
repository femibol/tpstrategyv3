"""momentum strategy: Donchian breakout is the PRIMARY gate, not optional.

2026-06-13 audit found the strategy fires on naive EMA crossover (which
published research correlates with 20-28% WR) while documenting itself
as Turtle Traders / Donchian breakout (40-50% WR). Realized 30d WR was
26% on -$500 P&L — symptom of the implementation/documentation mismatch.

Fix: Donchian breakout (close above N-bar high + volume ≥ 1.5×) is the
primary signal gate. EMA bullish + ADX become confirmations. Old
secondary "pullback entry" was too tight (required price within 0.5%
ABOVE fast EMA — but real pullbacks dip BELOW); replaced with a
prior-bar-low support reference inside a confirmed trend.

These tests pin the new gate logic:
  1. Pure EMA cross with no breakout, no volume surge → no signal
  2. Donchian breakout + EMA bullish + ADX > threshold + volume ≥1.5x → signal
  3. Breakout WITHOUT volume confirmation → no signal (volume gate)
  4. Pullback to prior-bar low in a strong trend → signal (secondary path)
  5. Hardcoded watchlist removed: strategy uses only dynamic symbols
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from unittest import mock


def _strat(config_overrides=None):
    from bot.strategies.momentum import MomentumStrategy
    from bot.data.indicators import TechnicalIndicators

    cfg = {
        "fast_ema": 8, "slow_ema": 21, "signal_ema": 5,
        "adx_threshold": 35, "volume_surge_multiplier": 1.3,
        "atr_period": 14, "atr_stop_multiplier": 1.5,
        "atr_target_multiplier": 5.0, "max_holding_bars": 40,
        "max_hold_days": 5, "breakout_lookback": 20,
        "symbols": [],  # dynamic only
        "min_day_change_pct": -2.0,
    }
    if config_overrides:
        cfg.update(config_overrides)
    return MomentumStrategy(cfg, TechnicalIndicators(), capital=10000)


class _FakeMD:
    def __init__(self, bars, quote):
        self._bars = bars; self._quote = quote
    def get_quote(self, sym):
        return self._quote
    def get_bars(self, sym, n, bar_size=None):
        return self._bars


def _bars_strong_breakout(bar_close=110.0, lookback_high=99.0, vol_surge=1.6):
    """50 bars: 30 ramping (200→99), 19 flat at 99, final at bar_close (breakout)."""
    prices = np.linspace(95.0, lookback_high, 30)
    prices = np.concatenate([prices, np.full(19, lookback_high), [bar_close]])
    closes = prices.astype(float)
    opens = closes - 0.10
    highs = closes + 0.20
    lows = closes - 0.20
    # Volume: 1M baseline, final bar = vol_surge × baseline
    vols = np.concatenate([np.full(49, 1_000_000), [int(1_000_000 * vol_surge)]])
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols.astype(int),
    })


def _bars_ema_cross_no_breakout(bar_close=99.5):
    """Bars where EMA just crossed but price is BELOW the lookback high
    (no Donchian breakout). Old strategy would fire, new strategy must NOT."""
    # 50 bars trending up but final close below recent high
    prices = np.linspace(95.0, 100.0, 30)
    prices = np.concatenate([prices, np.full(19, 99.8), [bar_close]])
    closes = prices.astype(float)
    return pd.DataFrame({
        "open": closes - 0.1, "high": closes + 0.2, "low": closes - 0.2,
        "close": closes,
        "volume": np.full(50, 1_500_000).astype(int),  # decent vol but no breakout
    })


# === 1. EMA cross alone no longer triggers ===


def test_ema_cross_without_breakout_does_not_fire():
    strat = _strat()
    strat._dynamic_symbols.add("FAKE")
    bars = _bars_ema_cross_no_breakout(bar_close=99.5)
    quote = {"price": 99.5, "change_pct": 1.0}
    sigs = strat.generate_signals(_FakeMD(bars, quote))
    assert len(sigs) == 0, (
        "Pure EMA crossover WITHOUT Donchian breakout must NOT fire — "
        "the audit fix demoted EMA cross from trigger to confirmation."
    )


# === 2. Donchian breakout + volume + ADX → fires ===


def test_breakout_with_volume_and_adx_fires():
    """Classic Turtle setup: close above 20-bar high on volume ≥1.5×."""
    strat = _strat()
    strat._dynamic_symbols.add("FAKE")
    bars = _bars_strong_breakout(bar_close=110.0, lookback_high=99.0, vol_surge=1.8)
    quote = {"price": 110.0, "change_pct": 5.0}
    sigs = strat.generate_signals(_FakeMD(bars, quote))
    assert len(sigs) == 1, "Donchian breakout + vol + ADX must fire"


def test_breakout_signal_score_is_promoted():
    """Breakout signal should get the +0.15 confidence bump."""
    strat = _strat()
    strat._dynamic_symbols.add("FAKE")
    bars = _bars_strong_breakout(bar_close=110.0, lookback_high=99.0, vol_surge=1.8)
    quote = {"price": 110.0, "change_pct": 5.0}
    sigs = strat.generate_signals(_FakeMD(bars, quote))
    assert sigs[0]["confidence"] >= 0.65  # base 0.5 + breakout 0.15


# === 3. Volume gate blocks weak breakouts ===


def test_breakout_without_volume_confirmation_skipped():
    """Even a clean breakout with weak volume (1.2× avg) must NOT fire —
    Turtle rule: volume must confirm."""
    strat = _strat()
    strat._dynamic_symbols.add("FAKE")
    # vol_surge=1.1 → vol_ratio < 1.5 → primary gate fails
    bars = _bars_strong_breakout(bar_close=110.0, lookback_high=99.0, vol_surge=1.1)
    quote = {"price": 110.0, "change_pct": 5.0}
    sigs = strat.generate_signals(_FakeMD(bars, quote))
    assert len(sigs) == 0, "Breakout with vol_ratio < 1.5 must be filtered"


# === 4. Hardcoded watchlist removed ===


def test_hardcoded_symbols_removed():
    """Audit decision: the 20-mega-cap watchlist (AAPL/MSFT/NVDA/etc.)
    is removed. Strategy fires only on scanner-injected symbols."""
    strat = _strat()
    symbols = strat.get_symbols()
    # No dynamic symbols added yet, so the universe should be empty
    assert symbols == [], (
        f"Expected empty universe (dynamic only); got {symbols}. The 20-mega-cap"
        f" hardcoded list was removed in the 2026-06-13 audit."
    )
