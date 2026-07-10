"""Per-strategy risk FLOOR — the lever that actually raises crypto spend.

2026-07-10 live-log finding: PR #248 raised mean_reversion's risk CEILING
to $150 but crypto trades still sized at ~$50 risk:

    Position size (crypto): PEPE-USD = $1,002.99 | Risk: $50.15
    Position size (crypto): TIA-USD  = $1,092.26 | Risk: $54.61

Root cause: per-strategy Kelly computes NEGATIVE for mean_reversion's
many-small-wins / fewer-bigger-losses profile (payoff ratio < 1 forces
Kelly f < 0 even at positive net EV — the strategy is +$838 over 8
weeks), so kelly_mult pins at the 0.25x floor → 0.25% × balance ≈ $50.
A ceiling above a Kelly-floored value is inert.

Fix: `risk.min_dollar_risk_per_strategy` — applied after the multiplier
stack, BEFORE the per-strategy ceiling (ceiling wins conflicts), and
bounded by the 2%-of-balance hard ceiling so a fat floor can never
out-risk account rules.
"""
from __future__ import annotations

from pathlib import Path

import yaml

SETTINGS = Path(__file__).parent.parent / "config" / "settings.yaml"
SIZER = Path(__file__).parent.parent / "bot" / "risk" / "position_sizer.py"


def _floor_then_cap(risk_dollars, balance, strat_floor, strat_cap):
    """Mirror of the shipped ordering in position_sizer.calculate()."""
    if strat_floor and strat_floor > 0 and risk_dollars < strat_floor:
        bounded_floor = min(float(strat_floor), balance * 0.02)
        if bounded_floor > risk_dollars:
            risk_dollars = bounded_floor
    if strat_cap and strat_cap > 0 and risk_dollars > strat_cap:
        risk_dollars = float(strat_cap)
    return risk_dollars


def test_floor_lifts_kelly_pinned_risk():
    """The live case: Kelly-floored $50 on a $21.3K account, floor $100."""
    out = _floor_then_cap(50.15, 21_300, strat_floor=100, strat_cap=150)
    assert out == 100.0, "floor must lift $50 Kelly-pinned risk to $100"


def test_ceiling_still_wins_over_floor():
    """Floor $200 + ceiling $150 → ceiling wins (floor applied first)."""
    out = _floor_then_cap(50.0, 21_300, strat_floor=200, strat_cap=150)
    assert out == 150.0


def test_floor_bounded_by_2pct_hard_ceiling():
    """$100 floor on a tiny $3K account → 2% = $60 bounds it."""
    out = _floor_then_cap(20.0, 3_000, strat_floor=100, strat_cap=150)
    assert out == 60.0, "floor must never exceed 2% of balance"


def test_no_floor_no_change():
    out = _floor_then_cap(50.0, 21_300, strat_floor=None, strat_cap=150)
    assert out == 50.0


def test_risk_above_floor_untouched():
    """A strategy already sizing above the floor is not pushed down."""
    out = _floor_then_cap(140.0, 21_300, strat_floor=100, strat_cap=150)
    assert out == 140.0


def test_source_floor_applied_before_ceiling():
    """Ordering pin: the floor block must precede the ceiling block so a
    misconfigured floor > ceiling resolves to the ceiling (safe), never
    the floor."""
    src = SIZER.read_text()
    i_floor = src.find("STRATEGY RISK FLOOR")
    i_cap = src.find("STRATEGY RISK CAP")
    assert 0 < i_floor < i_cap, (
        "floor must be applied before the per-strategy ceiling"
    )
    assert "min(float(strat_floor), balance * 0.02)" in src, (
        "floor must be hard-capped at 2% of balance"
    )


def test_yaml_floor_only_on_proven_strategy():
    cfg = yaml.safe_load(SETTINGS.read_text())
    floors = cfg["risk"].get("min_dollar_risk_per_strategy", {})
    assert floors.get("mean_reversion") == 100
    # A floor on a bleeder scales the bleed — only the proven engine gets one.
    for bleeder in ("momentum", "rvol_scalp", "rvol_momentum"):
        assert bleeder not in floors, (
            f"{bleeder} must NOT have a risk floor — unproven/bleeding lanes "
            f"stay Kelly-throttled"
        )


def test_yaml_crypto_notional_cap_raised():
    cfg = yaml.safe_load(SETTINGS.read_text())
    assert cfg["crypto"]["risk"]["max_position_size_pct"] == 0.15, (
        "crypto per-position cap must be 0.15 — at 0.12 the notional cap "
        "(~$2.5K on a $21K balance) clips the floored risk sizing"
    )
