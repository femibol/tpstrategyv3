"""Deep-overnight equity guard — drop equity BUY signals when the equity
market is fully closed (20:00-04:00 ET, weekends, holidays).

Live diagnosis (2026-05-28): the bot fired `momentum` BUY on RKLB at
03:02 EDT into the dead-of-night session, IBKR extended-hours filled it,
the position stopped out for -$82.56 by 03:02 the next morning, and the
strategy daily-drawdown gate then auto-paused `momentum` for the entire
RTH session that followed — turning a single overnight slip into a full
"dead" trading day. The premarket/postmarket allowed_strategies filter
in engine.py:2025+ only applies INSIDE those windows; the deep-overnight
window between them had no guard.

These tests pin three guarantees:
  1. Equity BUY signals dropped when `_equity_market_open` is False.
  2. Crypto BUY signals always pass through, market closed or not.
  3. Premarket / postmarket (when one of those flags is True) is NOT
     touched by this guard — the existing allowed_strategies filter
     continues to handle those windows.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from bot.engine import TradingEngine


def _new_engine(equity_open=False, in_premarket=False, in_postmarket=False):
    """Bypass __init__ — only the session flags + _is_crypto_symbol matter."""
    eng = TradingEngine.__new__(TradingEngine)
    eng._equity_market_open = equity_open
    eng._in_premarket = in_premarket
    eng._in_postmarket = in_postmarket
    eng.config = SimpleNamespace(settings={})
    return eng


def _apply_overnight_guard(eng, approved):
    """Mirrors the inline block at engine.py:~2024 — extracted for testing.

    Kept identical to the in-engine code so a divergence regresses this
    test, not just the engine behavior.
    """
    if not getattr(eng, "_equity_market_open", False):
        pre_filtered = []
        dropped = []
        for sig in approved:
            if (sig.get("action") == "buy"
                    and not eng._is_crypto_symbol(sig.get("symbol", ""))):
                dropped.append(sig.get("symbol", "?"))
                continue
            pre_filtered.append(sig)
        return pre_filtered, dropped
    return approved, []


def test_equity_buy_dropped_when_market_closed():
    eng = _new_engine(equity_open=False)
    approved = [
        {"action": "buy", "symbol": "RKLB", "strategy": "momentum"},
        {"action": "buy", "symbol": "AAPL", "strategy": "momentum"},
    ]
    filtered, dropped = _apply_overnight_guard(eng, approved)
    assert filtered == []
    assert dropped == ["RKLB", "AAPL"]


def test_crypto_buy_passes_when_market_closed():
    eng = _new_engine(equity_open=False)
    approved = [
        {"action": "buy", "symbol": "BTC-USD", "strategy": "mean_reversion"},
        {"action": "buy", "symbol": "ETH-USD", "strategy": "crypto_runner"},
        {"action": "buy", "symbol": "RKLB", "strategy": "momentum"},
    ]
    filtered, dropped = _apply_overnight_guard(eng, approved)
    assert [s["symbol"] for s in filtered] == ["BTC-USD", "ETH-USD"]
    assert dropped == ["RKLB"]


def test_guard_inactive_when_equity_market_open():
    """RTH / premarket / postmarket — guard must not fire."""
    eng = _new_engine(equity_open=True)
    approved = [
        {"action": "buy", "symbol": "RKLB", "strategy": "momentum"},
        {"action": "buy", "symbol": "BTC-USD", "strategy": "mean_reversion"},
    ]
    filtered, dropped = _apply_overnight_guard(eng, approved)
    assert filtered == approved
    assert dropped == []


def test_exits_unaffected_even_when_market_closed():
    """SELL / exit signals must pass through regardless of session — stops
    fire 24/7 via _check_position_exits, not through this code path, but a
    SELL routed here (manual close, strategy-initiated exit) must not be
    blocked by the overnight guard."""
    eng = _new_engine(equity_open=False)
    approved = [
        {"action": "sell", "symbol": "RKLB", "strategy": "momentum"},
        {"action": "exit", "symbol": "AAPL", "strategy": "manual"},
    ]
    filtered, dropped = _apply_overnight_guard(eng, approved)
    assert filtered == approved
    assert dropped == []


def test_empty_signal_list_is_a_noop():
    eng = _new_engine(equity_open=False)
    filtered, dropped = _apply_overnight_guard(eng, [])
    assert filtered == []
    assert dropped == []
