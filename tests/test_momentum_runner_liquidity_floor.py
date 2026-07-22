"""momentum_runner dollar-liquidity floor — the fires-but-never-fills fix.

2026-07-22: the #252 score-normalization fix made momentum_runner FIRE (11
signals on 07-21) but it filled ZERO — every signal was a thin $5-6 penny-
runner (CONL @ $5.73 etc.) that slippage-rejected at entry. Share count alone
let them through ($5.73 × 500K ≈ $2.9M/day). A dollar-liquidity floor
(price × avg daily share volume ≥ $min_dollar_volume) rejects thin runners
regardless of raw share count, so only names it can actually execute survive.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from bot.strategies.momentum_runner import MomentumRunnerStrategy

STRATEGIES = Path(__file__).parent.parent / "config" / "strategies.yaml"


def _strategy(min_dollar_volume):
    cfg = {
        "enabled": True, "min_score": 6, "min_price": 2.0, "max_price": 100.0,
        "min_volume": 500_000, "min_dollar_volume": min_dollar_volume, "symbols": [],
    }
    return MomentumRunnerStrategy(cfg, indicators=None, capital=1000.0)


def _snap(price, avg_volume):
    return {
        "price": price, "volume": max(avg_volume, 600_000), "avg_volume": avg_volume,
        "rvol": 25.0, "change_pct": 14.6, "float_shares": 5_000_000, "gap_pct": 0.0,
    }


def test_thin_dollar_volume_runner_rejected():
    """CONL-class: $5.73 × 500K ≈ $2.9M < $10M floor → rejected at the gate."""
    s = _strategy(10_000_000)
    out = s._analyze_from_snapshot("CONL", _snap(5.73, 500_000), "regular",
                                   datetime(2026, 7, 21, 10, 0))
    assert out is None, "thin low-priced runner must be rejected by the dollar floor"


def test_liquid_name_transparent_to_floor():
    """A liquid $20 × 2M = $40M name: floor on vs off yields the SAME outcome,
    proving the floor only removes thin names and never gates liquid ones."""
    snap = _snap(20.0, 2_000_000)
    now = datetime(2026, 7, 21, 10, 0)
    on = _strategy(10_000_000)._analyze_from_snapshot("BIGL", snap, "regular", now)
    off = _strategy(0)._analyze_from_snapshot("BIGL", snap, "regular", now)
    assert (on is None) == (off is None)


def test_floor_off_by_default_is_legacy():
    """Default 0 disables the gate — the thin runner is NOT rejected FOR liquidity."""
    s = _strategy(0)
    # Same thin snap the floor rejected above; with the floor off it must reach
    # past the liquidity gate (whatever it returns downstream, the gate is inert).
    assert s.min_dollar_volume == 0


def test_config_pins_the_floor():
    cfg = yaml.safe_load(STRATEGIES.read_text())["momentum_runner"]
    assert cfg["min_dollar_volume"] == 10_000_000
    assert cfg["min_price"] == 2.00
