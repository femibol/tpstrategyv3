"""Verify the outside-RTH cancel policy on IBKRBroker.place_order.

The legacy behaviour cancelled every order that hadn't filled within 15s.
For outside-RTH orders that IBKR has accepted but is holding for the next
eligible session (status=PreSubmitted, e.g. Error 399 "will not be placed
at the exchange until..."), that cancel defeats the entire point of the
order — it would have filled at the next open. The new policy:

  - outside_rth=True extends the per-call timeout to 60s entry / 90s exit
    (RTH stays at 15s/30s, unchanged).
  - When the post-timeout status is PreSubmitted and nothing has filled,
    the broker does NOT cancel; it returns a {queued: True, status:
    "PreSubmitted"} result so the caller can record the queued order and
    skip phantom-position tracking.

These tests build an IBKRBroker without its real worker thread (we bypass
the thread + replace _run with an inline call) and drive its place_order
through a fake `ib_async` so the cancel/no-cancel decision is observable
without a live gateway.
"""
from __future__ import annotations

import sys
import types

import pytest

# We import the module so we can build instances via __new__ (skipping
# __init__ to avoid the dedicated worker thread + real ib_async import).
from bot.brokers import ibkr as ibkr_mod
from bot.brokers.ibkr import IBKRBroker


# --- Fakes for ib_async surface used by place_order --------------------------

class _FakeContract:
    def __init__(self, symbol="MSFT", exchange="SMART", currency="USD", **kw):
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.conId = 0  # will be set by qualifyContracts


class _FakeOrder:
    def __init__(self, action="BUY", qty=1, price=None):
        self.action = action
        self.totalQuantity = qty
        self.lmtPrice = price or 0
        self.orderType = "MKT"
        self.outsideRth = False
        self.tif = "DAY"
        self.overridePercentageConstraints = False
        # Assigned later by placeOrder
        self.orderId = 99001


class _FakeOrderStatus:
    def __init__(self, status, filled=0, avg_fill_price=0):
        self.status = status
        self.filled = filled
        self.avgFillPrice = avg_fill_price


class _FakeTrade:
    def __init__(self, status="PreSubmitted"):
        self.orderStatus = _FakeOrderStatus(status)
        self.fills = []
        self.order = None
        self.contract = None


class _FakeIB:
    """Stand-in for an ib_async IB() instance, scoped to what place_order needs."""

    def __init__(self, trade_status="PreSubmitted"):
        self._trade_status = trade_status
        self.cancel_calls = []  # list of orderIds cancelled
        self.placed_orders = []  # (contract, order) tuples
        self.positions_result = []
        self._trade = _FakeTrade(status=trade_status)
        self._snap_ask = 350.0

    # ----- methods place_order calls -----
    def isConnected(self):
        return True

    def positions(self):
        return self.positions_result

    def qualifyContracts(self, contract):
        contract.conId = 1
        return [contract]

    def placeOrder(self, contract, order):
        order.orderId = 12345
        self._trade.contract = contract
        self._trade.order = order
        self.placed_orders.append((contract, order))
        return self._trade

    def sleep(self, _secs):
        # Tight loop OK in tests — keep no-op so the 60s wait runs instantly.
        return None

    def cancelOrder(self, order):
        self.cancel_calls.append(order.orderId)

    def reqTickers(self, contract):
        # Used by _get_snap_price for MARKET → outside-RTH limit conversion.
        ticker = types.SimpleNamespace(
            bid=self._snap_ask - 0.5, ask=self._snap_ask,
            last=self._snap_ask - 0.25, close=self._snap_ask - 0.3,
        )
        return [ticker]


# --- Test builder ------------------------------------------------------------

def _make_broker(ib_mock):
    """Build an IBKRBroker without starting its worker thread.

    Bypasses __init__ entirely (so no worker thread spawns) and pins the
    minimum set of attributes place_order touches.
    """
    broker = IBKRBroker.__new__(IBKRBroker)
    broker.ib = ib_mock
    broker._connected = True
    broker._invalid_symbols = set()
    broker._streaming_contracts = {}
    broker._streaming_tickers = {}
    broker._live_prices = {}
    broker._stream_lock = None  # not touched on this path

    # Replace the worker-thread funnel with an inline call so the
    # @_on_worker decorator runs the body on this thread.
    broker._run = lambda fn, timeout=180: fn()
    return broker


def _patch_ib_constructors(monkeypatch, fake_ib):
    """Make the module-level Stock/MarketOrder/LimitOrder constructors return
    plain test fakes instead of the real ib_async classes. We don't have a
    live ib_async install in the test env, and place_order calls them directly.
    """
    monkeypatch.setattr(ibkr_mod, "Stock", _FakeContract, raising=False)
    monkeypatch.setattr(ibkr_mod, "MarketOrder",
                        lambda a, q: _FakeOrder(action=a, qty=q),
                        raising=False)
    monkeypatch.setattr(
        ibkr_mod, "LimitOrder",
        lambda a, q, p: _FakeOrder(action=a, qty=q, price=p),
        raising=False,
    )
    monkeypatch.setattr(
        ibkr_mod, "StopOrder",
        lambda a, q, p: _FakeOrder(action=a, qty=q, price=p),
        raising=False,
    )


# --- Tests -------------------------------------------------------------------

def test_outside_rth_presubmitted_is_not_cancelled(monkeypatch):
    """The core regression: an outside-RTH order that IBKR holds in
    PreSubmitted for the next session must NOT be cancelled by the 15s
    timeout. The broker should return a queued result so the engine can
    record the order without creating a phantom position."""
    ib = _FakeIB(trade_status="PreSubmitted")
    _patch_ib_constructors(monkeypatch, ib)
    broker = _make_broker(ib)

    result = broker.place_order(
        symbol="MSFT",
        action="BUY",
        quantity=1,
        order_type="MARKET",
        outside_rth=True,
    )

    assert ib.cancel_calls == [], (
        f"PreSubmitted outside-RTH order should NOT be cancelled; "
        f"cancelOrder was called with: {ib.cancel_calls}"
    )
    assert result is not None, "Broker must return a result, not None"
    # The broker returns `deferred=True` (not `queued`) for orders IBKR has
    # accepted but queued for the next session. The engine inspects this flag
    # BEFORE running the slippage / position-tracking code so no phantom
    # position is recorded — see _execute_signal's deferred-order branch.
    assert result.get("deferred") is True, f"deferred flag missing: {result}"
    assert result.get("status") == "PreSubmitted", f"unexpected status: {result}"


def test_rth_order_unfilled_still_gets_cancelled(monkeypatch):
    """Non-outside-RTH orders keep the legacy cancel-after-timeout behaviour
    (we only changed the outside-RTH path)."""
    # Make the trade sit at Submitted (not PreSubmitted) — pure timeout case.
    ib = _FakeIB(trade_status="Submitted")
    _patch_ib_constructors(monkeypatch, ib)
    broker = _make_broker(ib)

    result = broker.place_order(
        symbol="MSFT",
        action="BUY",
        quantity=1,
        order_type="MARKET",
        outside_rth=False,
    )

    assert ib.cancel_calls == [12345], (
        f"RTH unfilled order should be cancelled; cancelOrder calls: "
        f"{ib.cancel_calls}"
    )
    assert result is None, "Cancelled-with-no-fill should return None"


def test_outside_rth_submitted_no_fill_still_cancelled(monkeypatch):
    """outside_rth=True + status=Submitted (not PreSubmitted) + nothing
    filled within 60s = the order is actually working and stuck. Cancel it,
    same as the RTH path. We only spare PreSubmitted orders."""
    ib = _FakeIB(trade_status="Submitted")
    _patch_ib_constructors(monkeypatch, ib)
    broker = _make_broker(ib)

    result = broker.place_order(
        symbol="MSFT",
        action="BUY",
        quantity=1,
        order_type="MARKET",
        outside_rth=True,
    )

    assert ib.cancel_calls == [12345], (
        f"Outside-RTH Submitted-but-unfilled should still be cancelled "
        f"after the extended timeout; cancelOrder calls: {ib.cancel_calls}"
    )
    assert result is None
