"""Shared fixtures for the AlgoBot test suite.

The risk + sizing modules pull settings off a `config` object via attribute
access (`config.risk_config`, `config.reserve_cash_pct`, etc). The real
`bot.config.Config` reads YAML + env vars, which is overkill for unit tests,
so we build a small attribute-bag stand-in.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pytest


_DEFAULT_RISK_CONFIG = {
    "max_positions": 5,
    "max_position_size_pct": 0.15,
    "risk_per_trade_pct": 0.01,
    "min_volume": 50000,
    "min_price": 0.50,
    "max_price": 99999.0,
    "max_correlated_positions": 2,
    "long_only": False,
    "portfolio_limits": {
        "max_single_name_pct": 0.25,
        "max_gross_exposure_pct": 1.50,
        "max_net_exposure_pct": 1.00,
        "max_loss_per_position_pct": 0.08,
    },
}

_DEFAULT_SETTINGS = {
    "crypto": {
        "risk": {"max_position_size_pct": 0.10},
        "symbols_suffix": ["-USD", "-USDT", "-BTC", "-ETH"],
    },
}


class FakeConfig:
    """Attribute-bag config that satisfies the surface RiskManager and
    PositionSizer touch. Tests can override fields after construction or
    pass overrides to the factory fixture."""

    def __init__(
        self,
        risk_config=None,
        settings=None,
        reserve_cash_pct=0.20,
        risk_per_trade=0.01,
        max_daily_loss=0.02,
        max_drawdown=0.10,
    ):
        self.risk_config = copy.deepcopy(risk_config or _DEFAULT_RISK_CONFIG)
        self.settings = copy.deepcopy(settings or _DEFAULT_SETTINGS)
        self.reserve_cash_pct = reserve_cash_pct
        self.risk_per_trade = risk_per_trade
        self.max_daily_loss = max_daily_loss
        self.max_drawdown = max_drawdown


@pytest.fixture
def config():
    """Default config for most tests. Tweak fields in-test for variants."""
    return FakeConfig()


@pytest.fixture
def config_factory():
    """Returns a callable for tests that need bespoke config setups."""
    def _make(**overrides):
        return FakeConfig(**overrides)
    return _make


@pytest.fixture
def base_signal():
    """A clean, approvable buy signal — tests mutate one field at a time."""
    return {
        "symbol": "AAPL",
        "action": "buy",
        "price": 150.0,
        "quantity": 10,
        "stop_loss": 145.0,
        "take_profit": 160.0,
        "confidence": 0.7,
        "timestamp": datetime.now(),
        "strategy": "momentum",
        "reason": "test signal",
    }


@pytest.fixture
def position_factory():
    """Build a position dict matching the engine's shape."""
    def _make(symbol="AAPL", entry=100.0, qty=10, current=None, direction="long"):
        return {
            "symbol": symbol,
            "entry_price": entry,
            "current_price": current if current is not None else entry,
            "quantity": qty,
            "direction": direction,
        }
    return _make


@pytest.fixture
def stale_timestamp():
    """A signal timestamp older than the 60s freshness window."""
    return datetime.now() - timedelta(seconds=120)
