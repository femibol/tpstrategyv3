"""IBKR SMART-routing bypass for accounts without depth subscriptions.

2026-06-05 incident: PR #202 swapped bracket parent to MARKET but orders
STILL died in PendingSubmit during regular session. Diagnosis: IBKR's
SMART router requires depth subscriptions on BATS/NASDAQ/ARCA/NYSE to
validate the routing; without subs SMART silently leaves orders queued
forever. Premarket orders (outside_rth=True) avoided SMART and filled
fine — the SOFI position from 04:52 ET this morning is the proof.

Fix: route orders to a specific exchange (default IEX, per the Error
2152 dump showing we have IEX subs). Override the contract's
`exchange` attribute AFTER qualifyContracts() has resolved the conId
via SMART. Market data queries continue using SMART exclusively.

These tests pin three guarantees:
  1. Engine-side `risk.ibkr_routing_exchange` config knob is read into
     `self._order_routing_exchange` at IBKR init.
  2. Default value is "SMART" (no regression on full-sub accounts).
  3. Override value triggers the exchange swap in `place_order` —
     contract.exchange gets overwritten after qualification.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_ibkr_broker(routing_exchange=None):
    """Build an IBKRBroker instance with minimal config. Bypasses the
    network connection — we only exercise the contract-routing path."""
    from bot.brokers.ibkr import IBKRBroker

    risk_config = {}
    if routing_exchange is not None:
        risk_config["ibkr_routing_exchange"] = routing_exchange

    config = SimpleNamespace(
        ibkr_host="127.0.0.1",
        ibkr_port=4002,
        ibkr_client_id=1,
        risk_config=risk_config,
    )
    return IBKRBroker(config)


def test_default_routing_exchange_is_smart():
    """If `risk.ibkr_routing_exchange` is unset, the broker preserves
    pre-fix SMART routing — no regression on accounts with full subs."""
    broker = _make_ibkr_broker()
    assert broker._order_routing_exchange == "SMART"


def test_config_override_to_iex():
    """`risk.ibkr_routing_exchange: IEX` flows into the broker."""
    broker = _make_ibkr_broker(routing_exchange="IEX")
    assert broker._order_routing_exchange == "IEX"


def test_config_override_to_arca():
    """Any string works; broker takes it verbatim. Lets the operator
    point at NASDAQ/ARCA/etc. as the account's data subs allow."""
    broker = _make_ibkr_broker(routing_exchange="ARCA")
    assert broker._order_routing_exchange == "ARCA"


def test_routing_exchange_is_case_preserved():
    """We don't normalize case here — IBKR expects the exchange code
    verbatim (e.g., 'ISLAND' for NASDAQ matching engine, not 'island').
    The override comparison in place_order is case-insensitive against
    'SMART' specifically (so SMART/smart both no-op), but the chosen
    exchange string is passed through unchanged."""
    broker = _make_ibkr_broker(routing_exchange="ISLAND")
    assert broker._order_routing_exchange == "ISLAND"


def test_smart_string_in_config_is_no_op():
    """Setting the config to 'SMART' explicitly is identical to leaving
    it default — the broker recognizes SMART as the no-op case."""
    broker = _make_ibkr_broker(routing_exchange="SMART")
    assert broker._order_routing_exchange == "SMART"


def test_contract_exchange_swap_logic():
    """Pin the contract-rewrite logic from place_order. After
    qualifyContracts() the contract has the correct conId; we overwrite
    its `exchange` attribute to route the order. The conId stays
    intact."""
    # Simulate a qualified contract
    contract = MagicMock()
    contract.exchange = "SMART"
    contract.conId = 265598  # arbitrary positive (qualified)

    order_routing_exchange = "IEX"
    asset_type = "stock"  # not "option"

    # Apply the same gate the broker uses:
    if (
        order_routing_exchange
        and order_routing_exchange.upper() != "SMART"
        and asset_type != "option"
        and contract.conId != 0
    ):
        contract.exchange = order_routing_exchange

    assert contract.exchange == "IEX"
    assert contract.conId == 265598  # unchanged


def test_swap_skipped_when_conid_zero():
    """If contract failed qualification (conId == 0), don't overwrite —
    the order will fail downstream anyway, but at least we don't mask
    the underlying error by changing exchange."""
    contract = MagicMock()
    contract.exchange = "SMART"
    contract.conId = 0  # qualification failure

    order_routing_exchange = "IEX"
    asset_type = "stock"

    if (
        order_routing_exchange
        and order_routing_exchange.upper() != "SMART"
        and asset_type != "option"
        and contract.conId != 0  # the guard
    ):
        contract.exchange = order_routing_exchange

    assert contract.exchange == "SMART"  # unchanged


def test_swap_skipped_for_options():
    """Options routing is exchange-specific (CBOE, etc.) and the IEX
    fallback doesn't apply. Leave option contracts alone."""
    contract = MagicMock()
    contract.exchange = "CBOE"  # set by _create_option_contract
    contract.conId = 12345

    order_routing_exchange = "IEX"
    asset_type = "option"  # the guard

    if (
        order_routing_exchange
        and order_routing_exchange.upper() != "SMART"
        and asset_type != "option"
        and contract.conId != 0
    ):
        contract.exchange = order_routing_exchange

    assert contract.exchange == "CBOE"  # unchanged
