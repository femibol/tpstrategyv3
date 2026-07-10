"""Close-path anti-collision guard — phantom prices must never reach the
P&L record.

2026-06-22 incident: JUP-USD closed during a container restart and booked
a -$1,049 stop_loss at exit=$0.000013. Coinbase's ticker namespace
collides — a delisted JUP token squats the symbol while the real Jupiter
(the asset TradersPost routes to) trades ~$0.21. PR #245 added an
anti-collision guard to the DASHBOARD display, but the close/record path
in `_close_position_inner` / `_partial_close_inner` had no such guard, so
the phantom price became a permanent fake loss in trade_history and
corrupted the drawdown stats.

`_sane_exit_price(symbol, candidate, pos)` is the shared guard: for a
crypto symbol, if the candidate price deviates > 80% from entry it returns
the entry price (flat P&L) instead. 80% (vs the dashboard's 50%) avoids
clipping legitimate crypto runners/dumpers — only egregious collision
cases trip it.

These tests pin the helper in isolation.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _make_engine():
    """Bind just `_sane_exit_price` + `_is_crypto_symbol` onto a stub."""
    from bot.engine import TradingEngine
    stub = SimpleNamespace(
        crypto_suffixes=["-USD", "-USDT", "-BTC", "-ETH"],
        config=SimpleNamespace(settings={}),  # _is_crypto_symbol reads config.settings
    )
    stub._is_crypto_symbol = TradingEngine._is_crypto_symbol.__get__(stub)
    stub._sane_exit_price = TradingEngine._sane_exit_price.__get__(stub)
    return stub


def test_jup_collision_falls_back_to_entry():
    """The literal incident: entry $0.2099, Coinbase collision $0.000013.
    99.99% deviation → return entry, P&L flat."""
    eng = _make_engine()
    pos = {"entry_price": 0.2099}
    out = eng._sane_exit_price("JUP-USD", 0.000013, pos)
    assert out == 0.2099, (
        "JUP-USD collision price should be rejected in favor of entry — "
        "otherwise it books a fake ~100% loss into trade_history."
    )


def test_normal_crypto_loss_passes_through():
    """A real -10% exit on crypto must NOT be clipped."""
    eng = _make_engine()
    pos = {"entry_price": 1.00}
    out = eng._sane_exit_price("ARB-USD", 0.90, pos)
    assert out == 0.90


def test_legitimate_crypto_runner_passes_through():
    """A real +70% runner is under the 80% threshold — trust it."""
    eng = _make_engine()
    pos = {"entry_price": 1.00}
    out = eng._sane_exit_price("WIF-USD", 1.70, pos)
    assert out == 1.70


def test_crypto_dump_just_under_threshold_passes():
    """-79% is a brutal but plausible crypto move — under 80%, trust it."""
    eng = _make_engine()
    pos = {"entry_price": 1.00}
    out = eng._sane_exit_price("PEPE-USD", 0.21, pos)
    assert out == 0.21


def test_crypto_dump_over_threshold_rejected():
    """-85% from entry trips the guard — almost certainly bad data."""
    eng = _make_engine()
    pos = {"entry_price": 1.00}
    out = eng._sane_exit_price("PEPE-USD", 0.15, pos)
    assert out == 1.00


def test_equity_never_guarded():
    """Equity symbols pass through untouched even at extreme deviation —
    a $50 stock printing $0.01 is a real (if catastrophic) equity event,
    not a Coinbase ticker collision."""
    eng = _make_engine()
    pos = {"entry_price": 50.0}
    out = eng._sane_exit_price("WYFL", 0.01, pos)
    assert out == 0.01


def test_missing_entry_price_passes_through():
    """No entry reference → can't judge; return candidate unchanged."""
    eng = _make_engine()
    assert eng._sane_exit_price("JUP-USD", 0.000013, {"entry_price": 0}) == 0.000013
    assert eng._sane_exit_price("JUP-USD", 0.000013, {}) == 0.000013


def test_none_candidate_passes_through():
    """None price → return None, let the caller's fallback handle it."""
    eng = _make_engine()
    assert eng._sane_exit_price("JUP-USD", None, {"entry_price": 0.21}) is None


def test_both_close_paths_call_the_guard():
    """Anti-regression: both _close_position_inner and _partial_close_inner
    must route their fetched price through _sane_exit_price. A future
    refactor that drops one re-opens the phantom-loss hole on that path."""
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    # Count the call sites (helper definition + 2 calls = 3 mentions min)
    assert src.count("_sane_exit_price") >= 3, (
        "Expected _sane_exit_price defined once and called in BOTH "
        "_close_position_inner and _partial_close_inner. One of the close "
        "paths is missing the guard."
    )
