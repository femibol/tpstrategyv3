"""Dashboard /api/status performance fix (2026-06-16 live diagnostic).

Symptom: every operator-facing tab except History showed empty / "No
data" cards. Backend APIs all returned the right JSON when probed
directly — but the dashboard's main update() loop fires
Promise.all([api('status'), api('positions'), api('trades'), ...])
across 17 endpoints. The slowest determines the resolve time, and
iOS Safari aborts the fetch on long-running requests, returning null
to the JS, which then renders empty state for every dependent card.

Diagnostic: timed each endpoint individually
   /api/status               200 18.328138s    ← culprit
   /api/scanner              500 0.003461s     ← also broken
   /api/positions            200 0.003331s
   /api/trades               200 0.004542s
   (others all sub-1s)

Root cause for the 18s: `_get_ibkr_features_status()` calls
`self.broker.get_realtime_pnl()` and `self.broker.get_open_orders()`
— both decorated with `@_on_worker` so they queue behind the IBKR
single-threaded worker thread which is busy serving 48 streaming
symbols. Each call waits ~9s for its turn.

Root cause for the 500: a single strategy's `get_scan_results()`
was throwing, and `get_scanner_data` had no per-strategy try/except,
so one bad apple 500'd the whole endpoint.

Fixes pinned by these tests:
  1. `_get_ibkr_features_status` short-TTL cache (5s).
  2. `get_scanner_data` per-strategy try/except so a single failing
     strategy can't take down the pane.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_engine_with_broker():
    """Minimal engine stub with a fake broker that tracks call count
    so we can assert the cache actually short-circuits."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    broker = MagicMock()
    broker.is_connected.return_value = True
    broker.get_realtime_pnl.return_value = {"daily": 12.5, "realized": 10.0, "unrealized": 2.5}
    broker.get_open_orders.return_value = []
    broker._live_bars = {}
    broker._news_callback = None
    eng.broker = broker
    return eng, broker


# === 1. IBKR features status cache ===


def test_ibkr_features_first_call_hits_broker():
    eng, broker = _make_engine_with_broker()
    out = eng._get_ibkr_features_status()
    assert out["active"] is True
    assert broker.get_realtime_pnl.call_count == 1
    assert broker.get_open_orders.call_count == 1


def test_ibkr_features_second_call_within_ttl_uses_cache():
    """Second call within TTL must NOT re-hit the broker. This is the
    whole point of the fix — the slow broker calls are what causes
    /api/status to take 18s."""
    eng, broker = _make_engine_with_broker()
    eng._get_ibkr_features_status()
    eng._get_ibkr_features_status()
    eng._get_ibkr_features_status()
    eng._get_ibkr_features_status()
    # Only the first call talks to the broker
    assert broker.get_realtime_pnl.call_count == 1
    assert broker.get_open_orders.call_count == 1


def test_ibkr_features_cache_expires_after_ttl():
    """After 5s+, cache must expire so the operator eventually sees
    fresh data."""
    eng, broker = _make_engine_with_broker()
    eng._get_ibkr_features_status()
    # Force-expire the cache by rewinding the cached timestamp
    eng._ibkr_features_cache["ts"] -= 10
    eng._get_ibkr_features_status()
    # Broker hit a second time
    assert broker.get_realtime_pnl.call_count == 2


def test_ibkr_features_cache_when_broker_disconnected():
    """Disconnected broker → no broker calls at all (the early-return
    path) but the cached `{active: False}` should also be served."""
    eng, broker = _make_engine_with_broker()
    broker.is_connected.return_value = False
    out1 = eng._get_ibkr_features_status()
    out2 = eng._get_ibkr_features_status()
    assert out1["active"] is False
    assert out2["active"] is False
    # Even disconnected status should be cached so repeated checks
    # don't keep calling is_connected (which itself may be slow)
    assert broker.is_connected.call_count == 1


def test_ibkr_features_cache_independent_per_engine():
    """Two engine instances must not share cache (would be a memory
    leak class bug)."""
    eng_a, broker_a = _make_engine_with_broker()
    eng_b, broker_b = _make_engine_with_broker()
    eng_a._get_ibkr_features_status()
    eng_b._get_ibkr_features_status()
    # Each engine called its own broker exactly once
    assert broker_a.get_realtime_pnl.call_count == 1
    assert broker_b.get_realtime_pnl.call_count == 1


# === 2. Scanner data resilience ===


def test_get_scanner_data_skips_failing_strategy():
    """A throwing strategy must NOT crash the whole pane — the
    previous code 500'd the endpoint on a single failure."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    good_strat = MagicMock()
    good_strat.get_scan_results.return_value = {"FOO": {"verdict": "BUY"}}
    bad_strat = MagicMock()
    bad_strat.get_scan_results.side_effect = RuntimeError("strategy boom")
    eng.strategies = {"good": good_strat, "bad": bad_strat}
    out = eng.get_scanner_data()
    # Good strategy still appears in the response
    assert "good" in out
    assert out["good"] == {"FOO": {"verdict": "BUY"}}
    # Bad strategy silently dropped, no exception raised
    assert "bad" not in out


def test_get_scanner_data_returns_empty_dict_when_all_fail():
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    bad = MagicMock(); bad.get_scan_results.side_effect = RuntimeError("nope")
    eng.strategies = {"bad1": bad, "bad2": bad}
    out = eng.get_scanner_data()
    assert out == {}


def test_get_scanner_data_skips_empty_scan_results():
    """A strategy with no scan results (empty dict / None) should be
    omitted, not included as an empty key."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    empty = MagicMock(); empty.get_scan_results.return_value = {}
    none_strat = MagicMock(); none_strat.get_scan_results.return_value = None
    good = MagicMock(); good.get_scan_results.return_value = {"FOO": {"x": 1}}
    eng.strategies = {"empty": empty, "none": none_strat, "good": good}
    out = eng.get_scanner_data()
    assert out == {"good": {"FOO": {"x": 1}}}
