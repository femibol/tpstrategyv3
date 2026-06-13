"""Distance-to-mean SELL exit for mean_reversion (Wave 2 audit fix).

Mean reversion is "buy below mean, sell at/above mean". The strategy
shipped only the overshoot exit (z ≥ +1.5 AND RSI > overbought),
making the SELL trigger require the position to swing all the way to
the OTHER extreme. 30-day half-life audit found 22 trades closed
mid-run in PROFIT because `max_hold` time_exit fired before that
overshoot ever materialized.

Fix: also exit when zscore re-enters the at-mean band (`exit_zscore`
default 0.0). Pairs with Wave 1 #3 (per-symbol edge filter):
together they make the strategy actually trade its premise.

  - exit_zscore: 0.0  (equity — exact mean)
  - exit_zscore_crypto: 0.3 (small offset → avoid 5m mean-drift churn)

These tests pin:
  1. Mean-cross fires SELL when held + zscore ≥ exit_zscore
  2. Below exit_zscore: no SELL (still in mean-reversion territory)
  3. Only fires for held symbols (no scanner ghost-rejections)
  4. Same-bar BUY + mean-cross does NOT fire (entry takes precedence)
  5. Higher-conviction overshoot path still fires when its conditions hit
  6. Config knob respected; crypto uses crypto-specific threshold
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.strategies.mean_reversion import MeanReversionStrategy


class _FakeIndicators:
    def __init__(self, rsi=50.0, atr=1.0):
        self._rsi = rsi
        self._atr = atr

    def rsi(self, closes, period=14):
        return self._rsi

    def atr(self, highs, lows, closes, period=14):
        return self._atr


class _FakeMarketData:
    def __init__(self, bars_by_symbol):
        self._bars = bars_by_symbol

    def get_bars(self, symbol, n=None):
        return self._bars.get(symbol)

    def get_volume(self, symbol):
        df = self._bars.get(symbol)
        return int(df["volume"].iloc[-1]) if df is not None else None


def _bars_at_zscore(z_target, mean=100.0, n=40):
    """Build 40 bars where the strategy's lookback-20 window has a
    known mean + std, and the FINAL bar sits at exactly z_target.

    Strategy uses `closes[-lookback:]` with lookback=20. So the last 20
    closes define mean/std. We construct 19 alternating ±1 values
    around `mean` (mean = `mean`, std ≈ 1.0), then solve for the 20th
    so the realized zscore equals z_target.

    The full 40-bar frame just pads the first 20 with the same
    alternation so all indicators see consistent data.
    """
    spread = 1.0
    # 19 alternating ±spread: 10 negative, 9 positive (or vice versa)
    base19 = [mean - spread, mean + spread] * 9 + [mean - spread]
    base19 = np.array(base19, dtype=float)
    # Iteratively solve for target so realized zscore matches z_target.
    target = mean + z_target  # initial guess
    for _ in range(60):
        window = np.append(base19, target)
        w_mean = window.mean()
        w_std = window.std()
        if w_std == 0:
            break
        actual_z = (target - w_mean) / w_std
        if abs(actual_z - z_target) < 1e-4:
            break
        # Push target in direction of error
        target += (z_target - actual_z) * w_std * 0.5
    # Pad to 40 bars (first 20 = repeat of base19+target19/2)
    padding = list(base19) + [mean]
    closes = np.array(padding + list(base19) + [target], dtype=float)
    opens = closes - 0.05
    highs = closes + 0.05
    lows = closes - 0.05
    volumes = np.full(len(closes), 1_000_000)
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    })


@pytest.fixture
def strategy():
    config = {
        "enabled": True, "symbols": ["FAKE"],
        "lookback_period": 20, "entry_zscore": -2.0,
        "exit_zscore": 0.0, "exit_zscore_crypto": 0.3,
        "rsi_oversold": 30, "rsi_overbought": 70,
        "bollinger_period": 20, "bollinger_std": 2.0,
        "max_holding_periods": 20,
    }
    return MeanReversionStrategy(config, _FakeIndicators(rsi=50.0), capital=10000)


# === 1. Mean-cross fires when held ===


def test_sell_at_mean_when_held(strategy):
    """Position held; price returned to mean (z ≈ 0). SELL fires."""
    md = _FakeMarketData({"FAKE": _bars_at_zscore(0.05)})
    strategy.set_held_symbols({"FAKE"})
    sigs = strategy.generate_signals(md)
    sells = [s for s in sigs if s["action"] == "sell"]
    assert len(sells) == 1
    assert "mean reached" in sells[0]["reason"].lower()


def test_sell_at_slight_overshoot_when_held(strategy):
    """z = +0.5: past the mean but well below the overshoot threshold —
    still fires the mean-cross SELL."""
    md = _FakeMarketData({"FAKE": _bars_at_zscore(0.5)})
    strategy.set_held_symbols({"FAKE"})
    sigs = strategy.generate_signals(md)
    assert any(s["action"] == "sell" for s in sigs)


# === 2. Below threshold: no SELL ===


def test_no_sell_when_still_below_mean(strategy):
    """z = -0.5: position still mean-reverting — don't exit prematurely."""
    md = _FakeMarketData({"FAKE": _bars_at_zscore(-0.5)})
    strategy.set_held_symbols({"FAKE"})
    sigs = strategy.generate_signals(md)
    assert not any(s["action"] == "sell" for s in sigs)


# === 3. Only for held symbols ===


def test_no_sell_when_symbol_not_held(strategy):
    """Scanner-discovered FAKE at mean — must not fire SELL the engine
    can't honor (no position)."""
    md = _FakeMarketData({"FAKE": _bars_at_zscore(0.5)})
    strategy.set_held_symbols(set())  # holds nothing
    sigs = strategy.generate_signals(md)
    assert not any(s["action"] == "sell" for s in sigs)


# === 4. Same-bar BUY suppression ===


def test_same_bar_buy_suppresses_mean_cross_sell():
    """If the live bar simultaneously produces a BUY entry, don't also
    fire a SELL exit — the BUY trigger means we're at the bottom, not
    the mean. (Edge case: contradictory signals shouldn't ping-pong.)"""
    config = {
        "enabled": True, "symbols": ["FAKE"],
        "lookback_period": 20, "entry_zscore": -2.0,
        "exit_zscore": -10.0,  # bizarre setting: would normally fire SELL constantly
        "rsi_oversold": 30, "rsi_overbought": 70,
        "bollinger_period": 20, "bollinger_std": 2.0,
        "max_holding_periods": 20,
    }
    strat = MeanReversionStrategy(config, _FakeIndicators(rsi=20.0), capital=10000)
    strat.set_held_symbols({"FAKE"})
    # z = -2.5 + bump live volume to clear the 1.1x volume gate on the entry path
    bars = _bars_at_zscore(-2.5)
    bars.loc[bars.index[-1], "volume"] = 2_000_000  # 2x avg → vol_ratio ≈ 2.0
    md = _FakeMarketData({"FAKE": bars})
    sigs = strat.generate_signals(md)
    sells = [s for s in sigs if s["action"] == "sell"]
    # Even with exit_zscore = -10 (always triggerable), no SELL because BUY won
    assert len(sells) == 0
    # And a BUY DID fire (sanity check on the test setup)
    assert any(s["action"] == "buy" for s in sigs)


# === 5. Overshoot path still works as legacy fallback ===


def test_overshoot_path_still_fires_when_mean_cross_disabled():
    """exit_zscore set absurdly high (mean-cross disabled) — the
    higher-conviction overshoot path still fires when its OWN
    conditions hit."""
    config = {
        "enabled": True, "symbols": ["FAKE"],
        "lookback_period": 20, "entry_zscore": -2.0,
        "exit_zscore": 99.0,  # disable the mean-cross
        "rsi_oversold": 30, "rsi_overbought": 70,
        "bollinger_period": 20, "bollinger_std": 2.0,
        "max_holding_periods": 20,
    }
    strat = MeanReversionStrategy(config, _FakeIndicators(rsi=75.0), capital=10000)
    strat.set_held_symbols({"FAKE"})
    # z=+2.5, RSI=75 (overbought): the OLD overshoot exit must still fire
    md = _FakeMarketData({"FAKE": _bars_at_zscore(2.5)})
    sigs = strat.generate_signals(md)
    sells = [s for s in sigs if s["action"] == "sell"]
    assert len(sells) == 1
    # Reason text from the OLD path (not "mean reached")
    assert "mean reached" not in sells[0]["reason"].lower()


# === 6. Config wiring ===


def test_exit_zscore_defaults():
    """Defaults are wired and crypto gets the positive offset."""
    s = MeanReversionStrategy({"lookback_period": 20, "symbols": []},
                              _FakeIndicators(), capital=10000)
    assert s.exit_zscore == 0.0
    assert s.exit_zscore_crypto == 0.3


def test_exit_zscore_config_override():
    s = MeanReversionStrategy(
        {"lookback_period": 20, "exit_zscore": 0.7,
         "exit_zscore_crypto": 1.0, "symbols": []},
        _FakeIndicators(), capital=10000,
    )
    assert s.exit_zscore == 0.7
    assert s.exit_zscore_crypto == 1.0
