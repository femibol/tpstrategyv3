"""CostModel — entry gate that subtracts realistic friction.

Verifies the ratio-based design: cheap-stock momentum (1200 bps edge,
~30 bps cost) trivially clears even though absolute cost looks large
relative to the stock price. Conversely a high-priced thin-edge signal
(50 bps edge, 12 bps cost) fails at 2× ratio (24 > 50? no — wait
24 < 50, that passes). Tighten the example: 20 bps edge vs 12 bps cost
fails at 2× ratio (24 > 20). That's the exact class of trade the gate
exists to kill.
"""
from __future__ import annotations

import copy

import pytest

from bot.risk.cost_model import CostModel


class _Cfg:
    """Minimal config surface CostModel reads."""

    def __init__(self, cost_model_settings=None):
        self.settings = {"cost_model": cost_model_settings or {}}


def test_disabled_passes_everything():
    """When cost model is off, gate is a no-op (returns pass)."""
    cm = CostModel(_Cfg({"enabled": False}))
    signal = {"price": 100.0, "take_profit": 100.01}  # 1 bp edge
    passed, reason = cm.passes(signal, "equity")
    assert passed
    assert "disabled" in reason


def test_round_trip_cost_equity_defaults():
    """Equity: 2 bp fee + 5 bp spread × 2.0 mult = 12 bp round-trip."""
    cm = CostModel(_Cfg({}))
    assert cm.round_trip_cost_bps("equity") == pytest.approx(12.0)


def test_round_trip_cost_crypto_defaults():
    """Crypto: 30 bp fee + 10 bp spread × 2.0 mult = 50 bp round-trip."""
    cm = CostModel(_Cfg({}))
    assert cm.round_trip_cost_bps("crypto") == pytest.approx(50.0)


def test_live_spread_overrides_default():
    """Live spread is honored when supplied (e.g. wide-spread microcap)."""
    cm = CostModel(_Cfg({}))
    # 2 bp fee + (50 bp live spread × 2 mult) = 102 bp
    assert cm.round_trip_cost_bps("equity", live_spread_bps=50) == pytest.approx(102.0)


def test_expected_edge_bps_from_take_profit():
    """edge = (tp - price) / price × 10000."""
    cm = CostModel(_Cfg({}))
    # $100 → $103 = 300 bp
    assert cm.expected_edge_bps({"price": 100, "take_profit": 103}) == pytest.approx(300)
    # $1.16 → $1.30 = ~1207 bp (the QNCX case)
    assert cm.expected_edge_bps({"price": 1.16, "take_profit": 1.30}) == pytest.approx(1207, abs=1)


def test_expected_edge_zero_when_no_take_profit_or_stop():
    """No edge data at all → 0 → fails closed."""
    cm = CostModel(_Cfg({}))
    assert cm.expected_edge_bps({"price": 100, "take_profit": 0, "stop_loss": 0}) == 0
    assert cm.expected_edge_bps({"price": 100}) == 0
    assert cm.expected_edge_bps({}) == 0


def test_stop_loss_distance_is_used_as_post_stretch_proxy():
    """Engine stretches R/R to 2:1 AFTER RiskManager runs. The pre-stretch
    take_profit reaching this gate understates the real target — falling
    back to stop_loss × 2 captures post-stretch reality. The ATOM-USD
    signal from 2026-05-18 had tp=$2.06 (100 bp) but stop=$1.94 (500 bp
    × 2 = 1000 bp); the model should return 1000, not 100."""
    cm = CostModel(_Cfg({}))
    signal = {"price": 2.0430, "take_profit": 2.0638, "stop_loss": 1.9408}
    # tp_edge ≈ 102 bp, sl_edge ≈ 1000 bp → max
    assert cm.expected_edge_bps(signal) == pytest.approx(1000, abs=5)


def test_stop_loss_alone_yields_edge():
    """Signal with stop_loss but no take_profit still gets a usable edge
    via stop × 2 (the engine will set a stretched target)."""
    cm = CostModel(_Cfg({}))
    signal = {"price": 100.0, "stop_loss": 97.0}  # 3% stop → 6% post-stretch
    assert cm.expected_edge_bps(signal) == pytest.approx(600)


def test_cheap_stock_momentum_passes():
    """The exact case the user runs: $1.16 stock targeting $1.30 (1207 bp
    edge) passes easily even at default 2× ratio against 12 bp cost.
    This is the test that ensures the gate doesn't kill cheap-stock
    momentum scalps."""
    cm = CostModel(_Cfg({}))
    signal = {"price": 1.16, "take_profit": 1.30}
    passed, reason = cm.passes(signal, "equity")
    assert passed, f"cheap-stock momentum should pass: {reason}"


def test_thin_edge_high_price_blocked():
    """A signal claiming only 20 bp edge with no stop can't cover 12 bp
    cost × 2 ratio (needs 24 bp). This is the trade the gate exists
    to kill — costs eat the edge in expectation. No stop_loss given,
    so the stretch fallback can't save it either."""
    cm = CostModel(_Cfg({}))
    signal = {"price": 100.0, "take_profit": 100.20}  # 20 bp, no stop
    passed, reason = cm.passes(signal, "equity")
    assert not passed
    assert "Cost gate" in reason
    assert "20bps" in reason and "24bps" in reason


def test_crypto_stretched_target_passes():
    """A typical bot crypto entry: take_profit stretched to 2:1 R/R gives
    ~10% edge (1000 bp); default crypto cost is 50 bp. Ratio 20×, well
    above 2× threshold."""
    cm = CostModel(_Cfg({}))
    # ATOM-USD case: $2.04 → $2.25
    signal = {"price": 2.0430, "take_profit": 2.2473}
    passed, reason = cm.passes(signal, "crypto")
    assert passed, f"stretched crypto target should pass: {reason}"


def test_ratio_threshold_configurable():
    """A stricter 5× ratio rejects what 2× would pass."""
    cm_2x = CostModel(_Cfg({"min_edge_cost_ratio": 2.0}))
    cm_5x = CostModel(_Cfg({"min_edge_cost_ratio": 5.0}))
    # 50 bp tp_edge, no stop_loss → sl_edge=0 → edge=50
    # 2× threshold = 24 (pass), 5× threshold = 60 (fail)
    signal = {"price": 100.0, "take_profit": 100.50}
    assert cm_2x.passes(signal, "equity")[0] is True
    assert cm_5x.passes(signal, "equity")[0] is False


def test_short_take_profit_below_price_uses_abs():
    """Short-side: take_profit below price still has positive |edge|.
    Bot is long-only today but the model shouldn't break if a sell
    signal slips through."""
    cm = CostModel(_Cfg({}))
    signal = {"price": 100.0, "take_profit": 95.0}  # short target
    assert cm.expected_edge_bps(signal) == pytest.approx(500)
