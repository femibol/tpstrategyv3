"""Per-strategy min_confidence floor in RiskManager.

Conservative dial 2026-05-18: scalp / momentum need higher confidence
than mean-reversion because their low-conf entries are the noise that
historically drives most losses. Mean-reversion stays at the loose
global floor because the underlying math naturally lands at 0.35-0.40
even on valid setups.

The per-strategy table wins over the global floor. Strategies absent
from the table fall back to the global value (no behaviour change for
unlisted strategies).
"""
from __future__ import annotations

import copy
from datetime import datetime

import pytest

from bot.risk.manager import RiskManager


def _make_signal(symbol="AAPL", action="buy", confidence=0.5, strategy="momentum"):
    return {
        "symbol": symbol,
        "action": action,
        "price": 100.0,
        "quantity": 10,
        "stop_loss": 97.0,
        "take_profit": 106.0,
        "confidence": confidence,
        "timestamp": datetime.now(),
        "strategy": strategy,
    }


def test_scalp_floor_blocks_below_055(config):
    """rvol_scalp default floor is 0.55. A 0.50-conf scalp signal must
    be rejected, even though it passes the global 0.35 floor."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal(strategy="rvol_scalp", confidence=0.50)
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert not passed
    assert "rvol_scalp" in reason
    assert "0.55" in reason


def test_momentum_floor_blocks_below_050(config):
    """momentum default floor is 0.50. 0.45-conf momentum is rejected."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal(strategy="momentum", confidence=0.45)
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert not passed
    assert "momentum" in reason
    assert "0.50" in reason


def test_mean_reversion_stays_at_loose_floor(config):
    """mean_reversion default floor is 0.40 — same as the practical
    low end of its scoring math. 0.42 should pass."""
    rm = RiskManager(copy.deepcopy(config))
    sig = _make_signal(strategy="mean_reversion", confidence=0.42)
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert passed, f"unexpected rejection: {reason}"


def test_unlisted_strategy_falls_back_to_global_floor(config):
    """A strategy not in the per-strategy table uses the global floor
    (default 0.35). 0.40 conf with no per-strategy entry must pass."""
    cfg = copy.deepcopy(config)
    cfg.risk_config["min_confidence_per_strategy"] = {"rvol_scalp": 0.55}
    rm = RiskManager(cfg)
    sig = _make_signal(strategy="custom_unlisted", confidence=0.40)
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert passed, f"unexpected rejection for unlisted strategy: {reason}"


def test_high_conf_signal_always_passes(config):
    """A 0.85-conf signal passes every threshold."""
    rm = RiskManager(copy.deepcopy(config))
    for strategy in ["rvol_scalp", "momentum", "mean_reversion"]:
        sig = _make_signal(strategy=strategy, confidence=0.85)
        passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
        assert passed, f"{strategy} 0.85 rejected: {reason}"


def test_per_strategy_table_override_via_config(config):
    """Caller can override defaults entirely via config (e.g. for tests
    or for users who want a different per-strategy split)."""
    cfg = copy.deepcopy(config)
    cfg.risk_config["min_confidence_per_strategy"] = {"momentum": 0.80}
    rm = RiskManager(cfg)
    sig = _make_signal(strategy="momentum", confidence=0.75)
    passed, reason = rm._check_all_rules(sig, positions={}, balance=100000)
    assert not passed
    assert "0.80" in reason
