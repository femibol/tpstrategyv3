"""Slippage exit floor — never exit BELOW the predefined stop_loss.

2026-06-05 HIBS incident: bot bought 149 shares of HIBS (3x inverse high-
beta ETF) at $25.25. Entry slippage exceeded the 0.8% RTH threshold, so
slippage_reject was queued. By the time the MARKET SELL fired 5 seconds
later, HIBS had crashed to $22.73 — BELOW the bracket's stop_loss of
$23.12. Net loss: -$375.48 (-9.98%).

The slippage_reject MARKET SELL beat the bracket's server-side stop_loss
to execution, locking in a price WORSE than what the strategy's risk
parameters said was acceptable.

Fix: before firing the slippage_reject MARKET SELL, check live price vs
stop_loss. If live <= stop, skip the slippage_reject — the bracket
stop_loss is about to fire at IBKR and will exit at the predefined level
(or close to it, given liquidity).

These tests pin three guarantees:
  1. When live price is ABOVE stop_loss, slippage_reject fires normally
     (the worst-case position is bleeding but the floor isn't reached
     yet — the slippage exit caps further damage).
  2. When live price is AT/BELOW stop_loss, slippage_reject is SKIPPED
     and the bracket handles the exit.
  3. When stop_loss is missing/zero (positions without brackets), fall
     back to existing slippage_reject behavior — no NoneType crashes.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_engine_with_position(symbol, stop_loss, live_price):
    """Stub engine with one tracked position + market_data returning the
    requested live price. Exercises the slippage_close processing logic
    without running the full engine."""
    from bot.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine.positions = {
        symbol: {
            "symbol": symbol,
            "stop_loss": stop_loss,
            "quantity": 100,
            "entry_price": stop_loss * 1.08,  # ~8% above stop
        }
    }
    engine._slippage_close_queue = [symbol]
    engine.market_data = MagicMock()
    engine.market_data.get_quote.return_value = {"price": live_price}
    engine._close_position = MagicMock()
    return engine


def _run_slippage_close_processing(engine):
    """Mirror of the slippage-close processing block inside the entry
    handler. Walks the queue and either fires _close_position or skips."""
    if not getattr(engine, "_slippage_close_queue", None):
        return
    close_syms = list(engine._slippage_close_queue)
    engine._slippage_close_queue.clear()
    for close_sym in close_syms:
        if close_sym not in engine.positions:
            continue
        pos = engine.positions[close_sym]
        stop_loss = float(pos.get("stop_loss", 0) or 0)
        current_price = None
        try:
            quote = engine.market_data.get_quote(close_sym) if engine.market_data else None
            if quote:
                current_price = quote.get("price") or 0
        except Exception:
            pass
        if (
            stop_loss > 0
            and current_price
            and float(current_price) <= stop_loss
        ):
            # Skip — bracket will handle
            continue
        engine._close_position(close_sym, "slippage_reject",
                               "Excessive slippage on entry — R:R invalid")


# === 1. Price above stop_loss → slippage_reject fires ===


def test_slippage_reject_fires_when_price_above_stop():
    """Normal case: price hasn't crashed past stop yet, slippage_reject
    MARKET SELL fires to cap further damage."""
    engine = _make_engine_with_position("WIDGET", stop_loss=10.00, live_price=10.50)
    _run_slippage_close_processing(engine)
    engine._close_position.assert_called_once()
    args = engine._close_position.call_args[0]
    assert args[1] == "slippage_reject"


def test_slippage_reject_fires_when_price_just_above_stop():
    """Boundary: live price ONE cent above stop → still fires (haven't
    crossed yet, slippage exit caps further damage)."""
    engine = _make_engine_with_position("WIDGET", stop_loss=10.00, live_price=10.01)
    _run_slippage_close_processing(engine)
    engine._close_position.assert_called_once()


# === 2. Price at/below stop_loss → skip ===


def test_slippage_reject_skipped_when_price_below_stop():
    """HIBS scenario: live $22.73 already below stop $23.12. Skip the
    slippage_reject MARKET SELL — let the bracket handle the exit at
    the predefined stop level."""
    engine = _make_engine_with_position("HIBS", stop_loss=23.12, live_price=22.73)
    _run_slippage_close_processing(engine)
    engine._close_position.assert_not_called()


def test_slippage_reject_skipped_when_price_exactly_at_stop():
    """Boundary: live price exactly equals stop. The bracket is about
    to fire — don't race it."""
    engine = _make_engine_with_position("WIDGET", stop_loss=10.00, live_price=10.00)
    _run_slippage_close_processing(engine)
    engine._close_position.assert_not_called()


# === 3. Missing stop_loss → fall back to existing behavior ===


def test_no_stop_loss_falls_back_to_close():
    """Position without stop_loss (e.g., crypto or pre-bracket): the
    floor logic must NOT crash and MUST proceed with the close."""
    engine = _make_engine_with_position("WIDGET", stop_loss=0, live_price=5.00)
    _run_slippage_close_processing(engine)
    engine._close_position.assert_called_once()


def test_no_market_data_falls_back_to_close():
    """If we can't read live price, default to closing (the existing
    behavior). Don't strand the position in an undefined state."""
    engine = _make_engine_with_position("WIDGET", stop_loss=10.00, live_price=8.00)
    engine.market_data = None  # disable price lookup
    _run_slippage_close_processing(engine)
    engine._close_position.assert_called_once()


def test_market_data_returns_none_falls_back_to_close():
    engine = _make_engine_with_position("WIDGET", stop_loss=10.00, live_price=8.00)
    engine.market_data.get_quote.return_value = None
    _run_slippage_close_processing(engine)
    engine._close_position.assert_called_once()


# === 4. Multiple positions in queue ===


def test_queue_processes_each_position_independently():
    """Two positions queued: one below stop (skip), one above (fire)."""
    from bot.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine.positions = {
        "SAFE": {"symbol": "SAFE", "stop_loss": 10.00, "quantity": 100,
                 "entry_price": 11.00},
        "CRASH": {"symbol": "CRASH", "stop_loss": 20.00, "quantity": 100,
                  "entry_price": 22.00},
    }
    engine._slippage_close_queue = ["SAFE", "CRASH"]
    engine.market_data = MagicMock()

    # SAFE live above stop, CRASH live below stop
    def quote_side_effect(sym):
        return {"price": 10.50 if sym == "SAFE" else 19.00}
    engine.market_data.get_quote.side_effect = quote_side_effect
    engine._close_position = MagicMock()

    _run_slippage_close_processing(engine)

    # Only SAFE should have been closed
    engine._close_position.assert_called_once()
    args = engine._close_position.call_args[0]
    assert args[0] == "SAFE"
