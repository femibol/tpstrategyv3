"""rvol_momentum disabled — the -$152 bleeder cut (2026-07-22).

Over the 10 days since 07-12, rvol_momentum was -$152 across 35 trades: the
single biggest loss source and net-negative in every measured window since
#237 throttled (rather than killed) it. Its entries on illiquid $5-6 runners
slippage-reject on entry, which also session-blocks the SAME symbols
momentum_runner then can't fill. Cut explicit — same treatment rvol_scalp got.
"""
from __future__ import annotations

from pathlib import Path

import yaml

STRATEGIES = Path(__file__).parent.parent / "config" / "strategies.yaml"


def test_rvol_momentum_disabled_and_zero_alloc():
    cfg = yaml.safe_load(STRATEGIES.read_text())
    assert cfg["rvol_momentum"]["enabled"] is False, "rvol_momentum must be disabled"
    assert cfg["allocation"]["rvol_momentum"] == 0.0, "rvol_momentum allocation must be zeroed"
