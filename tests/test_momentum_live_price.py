"""momentum signal must use LIVE quote price, not the bar's close.

Live bleed 2026-06-02: MRVU bar_close was $249.15 at 16:13. By 17:08
the live ask had climbed to $279 (+12%) on a real breakout. The momentum
strategy fired the same $249.15 signal every 3 minutes for 2 HOURS;
engine's drift gate correctly rejected each as "STALE SIGNAL SKIP".
Same story on MRVL ($290 → $314, +8%) and SOFI.

Root cause: `current_price = closes[-1]` (momentum.py:120 before this
PR) reads the most recent CLOSED 5-min bar. On a runner, live ask
drifts up while the bar refreshes slowly, so by signal-emit time the
price field is already several percent stale. The engine's drift gate
(1.6% max) then rejects every cycle while the strategy keeps re-
emitting the same anchor.

Fix: at the top of `_analyze_symbol`, pull `market_data.get_quote(symbol)`
and use the live price for `current_price`. Indicators (EMA/ADX/RSI)
still use the closed-bar `closes[]` array — those want closed-bar data.

These tests pin three guarantees:
  1. When live quote is FRESHER than bar close, signal uses live price
  2. When live quote is UNAVAILABLE (broker hiccup), falls back to bar
     close (don't fail the signal entirely on quote miss)
  3. stop_loss and take_profit are computed AGAINST the live price (so
     the engine's drift gate sees 0% staleness at emit time)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _strat():
    from bot.data.indicators import TechnicalIndicators
    from bot.strategies.momentum import MomentumStrategy

    cfg = {
        "fast_ema": 8, "slow_ema": 21, "adx_threshold": 25,
        "volume_surge_multiplier": 1.5, "atr_period": 14,
        "atr_stop_multiplier": 2.0, "atr_target_multiplier": 4.0,
        "max_holding_bars": 40, "breakout_lookback": 20,
    }
    return MomentumStrategy(cfg, TechnicalIndicators(), capital=10000)


class _FakeMarketData:
    def __init__(self, bars, quote_price=None):
        self._bars = bars
        self._quote_price = quote_price

    def get_quote(self, symbol):
        if self._quote_price is None:
            return None
        return {"price": self._quote_price}

    def get_bars(self, symbol, n, bar_size=None):
        return self._bars


def _make_breakout_bars(bar_close=249.15, lookback_high=240.0):
    """50 bars trending up into a breakout. Final bar closes at
    bar_close above the prior 20-bar high of lookback_high.
    Tuned to produce primary buy verdict (EMA bullish + breakout +
    strong ADX + vol surge)."""
    n = 50
    # Build a clean uptrend: prices ramp from 200 to lookback_high - 1
    # over the first 30 bars (well-defined ADX), then 19 bars at
    # lookback_high - 1 (flat = no false breakouts in lookback window),
    # final bar at bar_close (the breakout).
    prices = np.linspace(200.0, lookback_high - 1.0, 30)
    prices = np.concatenate([prices, np.full(19, lookback_high - 1.0), [bar_close]])
    closes = prices.astype(float)
    opens = closes - 0.10
    highs = closes + 0.20
    lows = closes - 0.20
    # Higher vol on final bar for vol_surge confirmation
    vols = np.concatenate([np.full(49, 1_000_000), [2_500_000]])
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols.astype(int),
    })


def test_signal_uses_live_quote_when_available():
    """When live quote diverges from bar close, signal MUST use live
    quote. This is the MRVU bug — strategy re-emits stale bar price
    while live ask climbs."""
    strat = _strat()
    strat._dynamic_symbols.add("MRVU")
    bars = _make_breakout_bars(bar_close=249.15, lookback_high=240.0)
    # Live price 5% above bar close — what MRVU did mid-day
    md = _FakeMarketData(bars, quote_price=262.50)
    sigs = strat.generate_signals(md)
    assert len(sigs) == 1, f"Expected 1 momentum signal; got {len(sigs)}"
    assert sigs[0]["price"] == pytest.approx(262.50), (
        f"Signal price must use live quote 262.50, not bar close 249.15. "
        f"Got {sigs[0]['price']}. This is the stale-signal bug from "
        f"engine.py drift gate."
    )


def test_signal_falls_back_to_bar_close_when_quote_missing():
    """If broker quote unavailable (transient hiccup, market data lag),
    fall back to bar close — don't kill the signal entirely."""
    strat = _strat()
    strat._dynamic_symbols.add("MRVU")
    bars = _make_breakout_bars(bar_close=249.15, lookback_high=240.0)
    md = _FakeMarketData(bars, quote_price=None)  # No quote available
    sigs = strat.generate_signals(md)
    assert len(sigs) == 1, "Should still fire signal when quote missing"
    assert sigs[0]["price"] == pytest.approx(249.15), (
        f"Without live quote, signal falls back to bar close. "
        f"Got {sigs[0]['price']}."
    )


def test_stop_loss_and_take_profit_computed_against_live_price():
    """stop_loss = current_price - atr_stop_mult × ATR. If we used the
    bar close for stop_loss but live price for the signal `price`, the
    R:R would be wildly off the moment the signal is emitted. Both must
    use the same anchor — the live price."""
    strat = _strat()
    strat._dynamic_symbols.add("MRVU")
    bars = _make_breakout_bars(bar_close=249.15, lookback_high=240.0)
    md = _FakeMarketData(bars, quote_price=262.50)
    sig = strat.generate_signals(md)[0]
    # stop_loss must be BELOW the live price (not below bar close which is
    # lower than live price — that would give a positive risk).
    assert sig["stop_loss"] < sig["price"], (
        f"stop_loss {sig['stop_loss']} must be below price {sig['price']}"
    )
    # take_profit must be ABOVE the live price
    assert sig["take_profit"] > sig["price"], (
        f"take_profit {sig['take_profit']} must be above price {sig['price']}"
    )
    # The stop should be close to live price (within ~10% — ATR-based),
    # not anchored to bar close. If stop were anchored to bar close
    # 249.15 with ATR ~1, stop would be ~247. With live 262.50 it should
    # be much higher (~260).
    assert sig["stop_loss"] > 255.0, (
        f"stop_loss {sig['stop_loss']} looks anchored to bar close, not "
        f"live price 262.50. The R:R fix didn't propagate."
    )
