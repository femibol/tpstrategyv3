"""Don't blacklist held symbols on conId==0 — 2026-05-14 SHOP regression.

place_order historically added any symbol to `_invalid_symbols` (a 2h
blacklist) when `qualifyContracts` returned with `conId == 0`. That's a
sound heuristic for genuine delistings but a wrong call when the API is
just glitching: a symbol whose position is currently held at the broker
is by definition tradeable.

2026-05-14 incident: SHOP closed at EOD with P&L -28%. The bot tried to
flatten the position but qualifyContracts returned `conId=0` on every
retry (thousands of "event loop is already running" errors from the old
ib_async path). SHOP got blacklisted, all close attempts refused for
the 2h TTL, and the position bled overnight until manual recovery the
next morning — an extra ~$20/share of damage on top of the original
loss.

PR #148's worker-thread isolation fixed the root cause of the event-loop
errors, but the blacklist still adds defense in depth so a future
qualification glitch can't strand the engine the same way.
"""
from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from bot.brokers import ibkr as ibkr_mod
from bot.brokers.ibkr import IBKRBroker


class _FakeContract:
    def __init__(self, symbol="DELISTED", exchange="SMART", currency="USD", **kw):
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.conId = 0  # qualify will leave it 0 in these tests


class _FakePosition:
    def __init__(self, symbol, qty):
        self.contract = types.SimpleNamespace(symbol=symbol)
        self.position = qty
        self.avgCost = 100.0


class _FakeIB:
    def __init__(self, positions=()):
        self._positions = list(positions)

    def isConnected(self):
        return True

    def qualifyContracts(self, contract):
        # Deliberately leave conId at 0 to simulate the glitch.
        return [contract]

    def positions(self):
        return list(self._positions)


def _make_broker(fake_ib):
    broker = IBKRBroker.__new__(IBKRBroker)
    broker.ib = fake_ib
    broker._connected = True
    broker._invalid_symbols = set()
    broker._streaming_contracts = {}
    broker._streaming_tickers = {}
    broker._live_prices = {}
    broker._stream_lock = None
    broker._run = lambda fn, timeout=180: fn()
    return broker


def _patch_constructors(monkeypatch):
    monkeypatch.setattr(ibkr_mod, "Stock", _FakeContract, raising=False)


def test_held_symbol_not_blacklisted_on_qualify_glitch(monkeypatch):
    """The 2026-05-14 SHOP path: conId==0 but the broker holds shares →
    do NOT add to _invalid_symbols. Otherwise the bot can't close."""
    ib = _FakeIB(positions=[_FakePosition("SHOP", 23)])
    _patch_constructors(monkeypatch)
    broker = _make_broker(ib)

    result = broker.place_order(
        symbol="SHOP", action="SELL", quantity=23,
        order_type="MARKET",
    )

    assert result is None, "place_order returns None on qualification failure"
    assert "SHOP" not in broker._invalid_symbols, (
        "Held symbol must NOT be blacklisted on conId==0 — that strands the "
        "close path. Got _invalid_symbols={}".format(broker._invalid_symbols)
    )


def test_unheld_symbol_blacklisted_on_qualify_failure(monkeypatch):
    """Genuine delisting case: conId==0 AND broker holds nothing → blacklist
    as before. The guard is targeted, not a removal of the heuristic."""
    ib = _FakeIB(positions=[])  # broker holds nothing
    _patch_constructors(monkeypatch)
    broker = _make_broker(ib)

    # SELL on a symbol we don't hold would normally be blocked by the
    # short-sell guard; use BUY to exercise the qualify→blacklist path.
    result = broker.place_order(
        symbol="DELISTED_CO", action="BUY", quantity=1,
        order_type="MARKET",
    )

    assert result is None
    assert "DELISTED_CO" in broker._invalid_symbols, (
        "Real conId==0 with no broker position must blacklist normally."
    )


def test_positions_call_failure_does_not_crash(monkeypatch):
    """If `self.ib.positions()` itself raises during the guard check, we
    must not crash — fall through to the original blacklist behaviour."""
    class _BrokenIB(_FakeIB):
        def positions(self):
            raise RuntimeError("API disconnect")

    ib = _BrokenIB()
    _patch_constructors(monkeypatch)
    broker = _make_broker(ib)

    result = broker.place_order(
        symbol="ACME", action="BUY", quantity=1, order_type="MARKET",
    )

    assert result is None
    # Falls through to legacy blacklist when we can't determine holdings.
    assert "ACME" in broker._invalid_symbols
