"""Mean reversion must not emit SELL signals for symbols the bot doesn't hold.

Without this gate, the scanner-discovered overbought-on-the-day stocks generate
sells every cycle. `risk_manager` rejects them ("No position to exit") which is
correct — but the signal slot is burned and the rejection log fills with noise.
On 2026-05-15 that pattern accounted for 115 of ~210 rejections in one morning.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.strategies.mean_reversion import MeanReversionStrategy


class _FakeIndicators:
    """Stub indicators — mean_reversion calls .rsi() and .atr()."""

    def __init__(self, rsi=75.0, atr=1.0):
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


def _overbought_bars(price=100.0, n=40):
    """Synthesize 40 bars where the last bar sits well above the mean — the
    setup that triggers the SELL branch (z-score >= |entry_zscore|, RSI high).
    """
    base = np.linspace(price - 5, price - 4, n - 1)
    closes = np.concatenate([base, [price + 5]])  # final bar way above mean
    opens = closes - 0.1
    highs = closes + 0.2
    lows = closes - 0.2
    volumes = np.full(n, 1_000_000)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}
    )


@pytest.fixture
def strategy():
    config = {
        "enabled": True,
        "symbols": ["FAKE"],
        "lookback_period": 20,
        "entry_zscore": -2.0,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "bollinger_period": 20,
        "bollinger_std": 2.0,
        "max_holding_periods": 20,
    }
    return MeanReversionStrategy(config, indicators=_FakeIndicators(), capital=10_000)


def test_sell_signal_suppressed_when_symbol_not_held(strategy):
    """Bot owns nothing → overbought FAKE must NOT produce a sell signal."""
    md = _FakeMarketData({"FAKE": _overbought_bars()})
    strategy.set_held_symbols(set())
    signals = strategy.generate_signals(md)
    assert not any(s["action"] == "sell" for s in signals), (
        "mean_reversion fired a sell for an unheld symbol — "
        "this is the bug that produced 115 ghost rejections on 2026-05-15"
    )


def test_sell_signal_emitted_when_symbol_is_held(strategy):
    """Bot owns FAKE → overbought condition produces the legitimate exit."""
    md = _FakeMarketData({"FAKE": _overbought_bars()})
    strategy.set_held_symbols({"FAKE"})
    signals = strategy.generate_signals(md)
    sells = [s for s in signals if s["action"] == "sell" and s["symbol"] == "FAKE"]
    assert len(sells) == 1, (
        f"expected one sell signal for held FAKE, got {len(sells)}"
    )


def test_held_symbols_unset_preserves_legacy_behavior(strategy):
    """Strategies whose engine never sets _held_symbols (None) keep firing —
    so a partial rollout / standalone use doesn't go silent. The risk_manager
    would still reject unheld sells; this just preserves the old behavior."""
    md = _FakeMarketData({"FAKE": _overbought_bars()})
    # Do NOT call set_held_symbols — _held_symbols stays None
    signals = strategy.generate_signals(md)
    assert any(s["action"] == "sell" for s in signals)
