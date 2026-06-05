"""Daily per-strategy signal/fill summary log.

Added in response to the 2026-05-27..06-04 silent-failure pattern: three
multi-day bleeds where strategies fired signals that never converted to
fills (QUALITY GATE score=0 bugs, wide-spread blocks). Each was visible
in per-cycle log noise but invisible in summary form.

The new _log_strategy_signal_summary() runs at EOD and prints one line
per strategy showing the day's delta — making "fired N, filled 0" jump
out at a glance instead of requiring greps across thousands of lines.

These tests pin three guarantees:
  1. Summary captures the delta since the last EOD (not cumulative-
     since-boot, which would drift higher every day).
  2. "FIRED-BUT-NEVER-FILLED" marker fires when a strategy has signals
     but zero conversions — the failure mode we care about.
  3. First call post-boot reports cumulative-since-boot (no prior
     snapshot to compute delta against).
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace


class _StubStrategy:
    def __init__(self, fired=0, filled=0):
        self.signals_generated = fired
        self.trades_taken = filled


def _make_engine(strategies):
    from bot.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine.strategies = strategies
    return engine


def _capture_log(caplog, level=logging.INFO):
    caplog.set_level(level, logger="trading_bot.engine")
    return caplog


def test_summary_includes_every_strategy(caplog):
    engine = _make_engine({
        "momentum": _StubStrategy(fired=5, filled=2),
        "mean_reversion": _StubStrategy(fired=12, filled=4),
    })
    _capture_log(caplog)
    engine._log_strategy_signal_summary()
    text = caplog.text
    assert "momentum" in text
    assert "mean_reversion" in text
    assert "fired=  5" in text
    assert "filled=  2" in text


def test_first_call_reports_cumulative_since_boot(caplog):
    """No prior snapshot → first call reports cumulative counters."""
    engine = _make_engine({
        "momentum": _StubStrategy(fired=10, filled=3),
    })
    _capture_log(caplog)
    engine._log_strategy_signal_summary()
    assert "fired= 10" in caplog.text


def test_subsequent_call_reports_delta_only(caplog):
    """After first EOD baselines the counters, the next call must report
    only the delta — that's the day's contribution."""
    strat = _StubStrategy(fired=10, filled=3)
    engine = _make_engine({"momentum": strat})

    # First call: baseline set, reports cumulative (10/3)
    engine._log_strategy_signal_summary()
    # Bump counters as if a new trading day produced 4 new fires, 1 fill
    strat.signals_generated = 14
    strat.trades_taken = 4

    caplog.clear()
    _capture_log(caplog)
    engine._log_strategy_signal_summary()
    # Delta should be 4 fired, 1 filled — not cumulative 14/4
    assert "fired=  4" in caplog.text, f"Expected delta=4, got: {caplog.text}"
    assert "filled=  1" in caplog.text


def test_fired_but_never_filled_marker(caplog):
    """The whole point of this feature: spotlight strategies that fire
    signals but produce no fills (QUALITY GATE bug pattern)."""
    engine = _make_engine({
        "low_float_catalyst": _StubStrategy(fired=8, filled=0),
        "mean_reversion": _StubStrategy(fired=10, filled=5),
    })
    _capture_log(caplog)
    engine._log_strategy_signal_summary()
    # The marker should appear on low_float, not on mean_reversion
    text = caplog.text
    # Split into per-strategy lines for precision
    lfc_line = [l for l in text.splitlines() if "low_float_catalyst" in l]
    mr_line = [l for l in text.splitlines() if "mean_reversion" in l]
    assert lfc_line, "low_float_catalyst should appear in summary"
    assert "FIRED-BUT-NEVER-FILLED" in lfc_line[0]
    assert mr_line and "FIRED-BUT-NEVER-FILLED" not in mr_line[0]


def test_silent_strategy_marker(caplog):
    """A strategy with zero signals and zero fills gets (silent) — not
    the FIRED-BUT-NEVER-FILLED warning (that would be noise on benign
    no-activity periods like weekends)."""
    engine = _make_engine({
        "options_momentum": _StubStrategy(fired=0, filled=0),
    })
    _capture_log(caplog)
    engine._log_strategy_signal_summary()
    text = caplog.text
    assert "(silent)" in text
    assert "FIRED-BUT-NEVER-FILLED" not in text


def test_conversion_pct_shown_when_signals_filled(caplog):
    engine = _make_engine({
        "momentum": _StubStrategy(fired=10, filled=2),
    })
    _capture_log(caplog)
    engine._log_strategy_signal_summary()
    assert "20% conversion" in caplog.text


def test_no_strategies_returns_silently(caplog):
    engine = _make_engine({})
    _capture_log(caplog)
    engine._log_strategy_signal_summary()
    # No header logged when there are no strategies — keeps EOD log clean
    assert "STRATEGY SIGNAL SUMMARY" not in caplog.text


def test_rows_sorted_by_fired_desc(caplog):
    """Most-active strategy at the top — operator's eye lands on the
    noisy ones first."""
    engine = _make_engine({
        "quiet": _StubStrategy(fired=1, filled=0),
        "noisy": _StubStrategy(fired=50, filled=10),
        "medium": _StubStrategy(fired=15, filled=3),
    })
    _capture_log(caplog)
    engine._log_strategy_signal_summary()
    lines = [l for l in caplog.text.splitlines() if "fired=" in l]
    # noisy first, medium second, quiet last
    order = [next(name for name in ("noisy", "medium", "quiet") if name in l)
             for l in lines]
    assert order == ["noisy", "medium", "quiet"]
