"""Per-asset-class sub-caps so crypto can't monopolize the slot budget.

2026-05-18 incident: AMZN entry signals were rejected for ~45 minutes
overnight with "Max positions reached (7)" while 7 crypto orphans (later
identified as the ghost-detector misfire — see test_ghost_position_detection)
held every slot. Even after the orphans are cleaned up, the structural
issue remains: crypto runs 24/7 while equity has a finite RTH window,
so overnight crypto fill naturally starves equity at open.

Fix: `max_crypto_positions` and `max_equity_positions` sub-caps in
RiskManager. Either can be ≤ `max_positions`; the smaller value is the
binding constraint for that class.
"""
from __future__ import annotations

import copy
from datetime import datetime

import pytest

from bot.risk.manager import RiskManager


def _make_signal(symbol, action="buy", price=100.0, quantity=1):
    return {
        "symbol": symbol,
        "action": action,
        "price": price,
        "quantity": quantity,
        "stop_loss": price * 0.97,
        "take_profit": price * 1.06,
        "confidence": 0.7,
        "timestamp": datetime.now(),
        "strategy": "test",
    }


def _make_positions(symbols):
    return {s: {"symbol": s, "quantity": 1, "entry_price": 100.0,
                "direction": "long", "stop_loss": 97.0}
            for s in symbols}


def test_crypto_subcap_blocks_overflow_crypto(config):
    """7 crypto held + crypto_cap=5 should block new crypto entry, even
    though global max_positions still has room."""
    cfg = copy.deepcopy(config)
    cfg.risk_config["max_positions"] = 10
    cfg.risk_config["max_crypto_positions"] = 5
    cfg.risk_config["max_equity_positions"] = 10
    rm = RiskManager(cfg)

    positions = _make_positions([
        "BTC-USD", "ETH-USD", "SOL-USD", "DOT-USD",
        "ICP-USD",  # 5 crypto already → at sub-cap
        "MSFT", "AAPL",  # 2 equity, room left globally
    ])
    sig = _make_signal("ATOM-USD")
    passed, reason = rm._check_all_rules(sig, positions, balance=100000)
    assert not passed
    assert "Crypto sub-cap" in reason


def test_crypto_subcap_doesnt_block_equity(config):
    """5 crypto + crypto_cap=5 means crypto is capped, but equity entries
    should still pass (equity cap not hit, global cap not hit)."""
    cfg = copy.deepcopy(config)
    cfg.risk_config["max_positions"] = 10
    cfg.risk_config["max_crypto_positions"] = 5
    cfg.risk_config["max_equity_positions"] = 6
    rm = RiskManager(cfg)

    positions = _make_positions([
        "BTC-USD", "ETH-USD", "SOL-USD", "DOT-USD", "ICP-USD",
        "MSFT", "AAPL",
    ])
    sig = _make_signal("AMZN")
    passed, reason = rm._check_all_rules(sig, positions, balance=100000)
    assert passed, f"unexpected rejection: {reason}"


def test_equity_subcap_blocks_overflow_equity(config):
    """Mirror case: equity full at its sub-cap, new equity rejected."""
    cfg = copy.deepcopy(config)
    cfg.risk_config["max_positions"] = 10
    cfg.risk_config["max_crypto_positions"] = 10
    cfg.risk_config["max_equity_positions"] = 3
    rm = RiskManager(cfg)

    positions = _make_positions(["MSFT", "AAPL", "AMZN"])
    sig = _make_signal("GOOGL")
    passed, reason = rm._check_all_rules(sig, positions, balance=100000)
    assert not passed
    assert "Equity sub-cap" in reason


def test_no_subcap_when_class_cap_equals_global(config):
    """If max_*_positions == max_positions (default), no class-specific
    block is added — preserves legacy behaviour when caller doesn't
    configure sub-caps."""
    cfg = copy.deepcopy(config)
    cfg.risk_config["max_positions"] = 10
    # Don't set max_*_positions — defaults fall back to max_positions.
    rm = RiskManager(cfg)

    positions = _make_positions([
        "BTC-USD", "ETH-USD", "SOL-USD", "DOT-USD",
        "ICP-USD", "ATOM-USD", "LINK-USD",
    ])
    sig = _make_signal("XRP-USD")
    passed, reason = rm._check_all_rules(sig, positions, balance=100000)
    assert passed, f"unexpected rejection without sub-caps: {reason}"


def test_global_cap_still_binding(config):
    """Sub-caps don't disable the global max_positions cap — both apply."""
    cfg = copy.deepcopy(config)
    cfg.risk_config["max_positions"] = 5
    cfg.risk_config["max_crypto_positions"] = 10  # sub-cap loose
    cfg.risk_config["max_equity_positions"] = 10
    rm = RiskManager(cfg)

    positions = _make_positions(["BTC-USD", "ETH-USD", "MSFT", "AAPL", "AMZN"])
    sig = _make_signal("SOL-USD")
    passed, reason = rm._check_all_rules(sig, positions, balance=100000)
    assert not passed
    assert "Max positions reached" in reason
