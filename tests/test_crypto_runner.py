"""Crypto Runner strategy — live-pump catcher on crypto.

Mirrors low_float_catalyst's "hard stop, hard TP, time stop, no trail"
shape on crypto. Entry: 1h move ≥ 5% AND RVOL ≥ 3x on the last 5-min bar.
New-entrant boost: symbols in scanner.new_entrants() get a relaxed 1h
threshold (default 0.5x) so we catch the first 30 min of a fresh run.

These tests pin the four guarantees the runner lane needs:
  1. Skip non-crypto symbols entirely (equity-only filter at entry).
  2. Reject below 1h-change threshold; accept above it.
  3. New entrants get the relaxed threshold.
  4. Signal carries the hard stop / hard TP / no-trail / no-server-bracket
     wiring the engine expects.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from bot.strategies.crypto_runner import CryptoRunnerStrategy


class _FakeMarketData:
    """Minimal market_data stub — only `get_bars` is called."""

    def __init__(self, bars_by_symbol):
        self._bars = bars_by_symbol

    def get_bars(self, symbol, n):
        df = self._bars.get(symbol)
        if df is None:
            return None
        return df.tail(n)


def _mk_bars(closes, volumes):
    """Build a tiny OHLCV DataFrame; only close + volume are read."""
    return pd.DataFrame({
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": volumes,
    })


def _strat(**overrides):
    cfg = {
        "min_1h_change_pct": 5.0,
        "min_rvol": 3.0,
        "hard_stop_pct": 0.06,
        "hard_target_pct": 0.15,
        "max_hold_minutes": 240,
        "max_trades_per_day": 8,
        "max_position_pct": 0.04,
        "new_entrant_threshold_mult": 0.5,
        "require_new_entrant": False,
    }
    cfg.update(overrides)
    return CryptoRunnerStrategy(cfg, indicators=None, capital=1000)


def _flat_then_pump_bars(start=1.00, pump_pct=0.08, n=25, vol_mult=10):
    """N flat bars, then the last one ramps `pump_pct` above the anchor 60min
    back AND spikes volume to `vol_mult`× the prior avg."""
    closes = [start] * (n - 1) + [start * (1 + pump_pct)]
    vols = [1000] * (n - 1) + [1000 * vol_mult]
    return _mk_bars(closes, vols)


# ---------------------------------------------------------------------------
# Universe filtering
# ---------------------------------------------------------------------------

def test_add_dynamic_symbols_filters_equity_out():
    s = _strat()
    s.add_dynamic_symbols(["BTC-USD", "AAPL", "ETH-USD", "TSLA", "NEAR-USD"])
    assert s._dynamic_symbols == {"BTC-USD", "ETH-USD", "NEAR-USD"}


def test_equity_symbol_passed_through_get_symbols_still_rejected_at_entry():
    """Defense in depth: even if a non-crypto name slips through
    add_dynamic_symbols, the per-symbol gate rejects it."""
    s = _strat()
    s.symbols = ["AAPL"]  # static-symbol bypass
    md = _FakeMarketData({"AAPL": _flat_then_pump_bars()})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    assert signals == []


# ---------------------------------------------------------------------------
# Entry gates
# ---------------------------------------------------------------------------

def test_signal_fires_when_1h_change_and_rvol_clear_threshold():
    s = _strat()
    s.add_dynamic_symbols(["WLD-USD"])
    md = _FakeMarketData({"WLD-USD": _flat_then_pump_bars(pump_pct=0.08, vol_mult=10)})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    assert len(signals) == 1
    sig = signals[0]
    assert sig["symbol"] == "WLD-USD"
    assert sig["action"] == "buy"
    assert sig["strategy"] == "crypto_runner"


def test_signal_blocked_when_1h_change_below_threshold():
    s = _strat()
    s.add_dynamic_symbols(["WLD-USD"])
    md = _FakeMarketData({"WLD-USD": _flat_then_pump_bars(pump_pct=0.02, vol_mult=10)})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    assert signals == []
    assert s.scan_results["WLD-USD"]["status"] == "wait_1h_change"


def test_signal_blocked_when_rvol_below_threshold():
    s = _strat()
    s.add_dynamic_symbols(["WLD-USD"])
    md = _FakeMarketData({"WLD-USD": _flat_then_pump_bars(pump_pct=0.08, vol_mult=1)})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    assert signals == []
    assert s.scan_results["WLD-USD"]["status"] == "wait_rvol"


def test_signal_blocked_when_insufficient_bars():
    s = _strat()
    s.add_dynamic_symbols(["WLD-USD"])
    # 12 bars < the 13 required for a 60-min anchor + current bar.
    bars = _mk_bars([1.0] * 11 + [1.10], [1000] * 11 + [10000])
    md = _FakeMarketData({"WLD-USD": bars})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    assert signals == []


# ---------------------------------------------------------------------------
# New-entrant boost
# ---------------------------------------------------------------------------

def test_new_entrant_gets_relaxed_1h_threshold():
    """+3% 1h move would normally be rejected (below 5% floor) but a
    new entrant clears it (0.5 × 5% = 2.5% floor)."""
    s = _strat()
    s.add_dynamic_symbols(["DRIFT-USD"])
    md = _FakeMarketData({"DRIFT-USD": _flat_then_pump_bars(pump_pct=0.03, vol_mult=10)})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value={"DRIFT-USD"}):
        signals = s.generate_signals(md)
    assert len(signals) == 1
    assert "new entrant" in signals[0]["reason"]
    assert signals[0]["confidence"] >= 0.5


def test_non_new_entrant_at_same_pump_still_rejected():
    """Same +3% pump — without the new-entrant tag it's below the 5%
    floor and must be rejected."""
    s = _strat()
    s.add_dynamic_symbols(["DRIFT-USD"])
    md = _FakeMarketData({"DRIFT-USD": _flat_then_pump_bars(pump_pct=0.03, vol_mult=10)})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    assert signals == []


def test_require_new_entrant_strict_mode():
    """With require_new_entrant=true, full-threshold non-entrants are
    still rejected — only new entrants ever enter."""
    s = _strat(require_new_entrant=True)
    s.add_dynamic_symbols(["DRIFT-USD"])
    # Big +8% pump and 10x RVOL — would pass normal-mode easily.
    md = _FakeMarketData({"DRIFT-USD": _flat_then_pump_bars(pump_pct=0.08, vol_mult=10)})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    assert signals == []


# ---------------------------------------------------------------------------
# Signal contract (engine-facing)
# ---------------------------------------------------------------------------

def test_signal_carries_engine_required_fields():
    s = _strat()
    s.add_dynamic_symbols(["WLD-USD"])
    md = _FakeMarketData({"WLD-USD": _flat_then_pump_bars(pump_pct=0.08, vol_mult=10)})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        sig = s.generate_signals(md)[0]
    # The runner playbook: hard stop + hard TP + time stop + NO trail +
    # NO server bracket (TradersPost/Alpaca crypto path doesn't accept
    # IBKR-style brackets).
    assert sig["use_server_bracket"] is False
    assert sig["trailing_stop_pct"] == 0
    assert sig["max_hold_bars"] == 240
    # Stop/TP math: entry 1.08, stop 1.08 × 0.94, target 1.08 × 1.15.
    entry = sig["price"]
    assert sig["stop_loss"] == pytest.approx(entry * 0.94, rel=1e-3)
    assert sig["take_profit"] == pytest.approx(entry * 1.15, rel=1e-3)


def test_held_symbols_skipped():
    s = _strat()
    s.add_dynamic_symbols(["WLD-USD"])
    s._held_symbols = {"WLD-USD"}
    md = _FakeMarketData({"WLD-USD": _flat_then_pump_bars(pump_pct=0.08, vol_mult=10)})
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    # Already held — don't double-up.
    assert signals == []


def test_daily_cap_caps_emitted_signals():
    s = _strat(max_trades_per_day=2)
    # 5 candidates, all firing — only 2 should come out.
    syms = ["WLD-USD", "DRIFT-USD", "JTO-USD", "ENA-USD", "PYTH-USD"]
    s.add_dynamic_symbols(syms)
    md = _FakeMarketData({
        sym: _flat_then_pump_bars(pump_pct=0.08, vol_mult=10) for sym in syms
    })
    with mock.patch("bot.strategies.crypto_runner.CryptoRunnerStrategy._new_entrants",
                    return_value=set()):
        signals = s.generate_signals(md)
    assert len(signals) == 2
