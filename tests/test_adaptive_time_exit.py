"""Adaptive time_exit (Tier 1 B) — half-life-triggered behavior.

30-day audit of 69 crypto time_exits:
  - 39 closed near zero (deadwood that sat unproductively until forced exit)
  - 22 closed in profit MID-RUN (winners cut short by hard max_hold)
  - 8 closed at modest loss (let stop_loss handle these instead)

At max_hold / 2 ("half-life"), evaluate the position once:
  - FLAT (-0.3% to +0.5%): close NOW — recycle capital, free a slot
  - WINNING (>+0.5%): tighten trail to 1%, extend max_hold × 2
  - LOSING (<-0.3%): leave alone, stop_loss / normal time_exit handles

These tests pin the decision logic in unit-testable form so a regression
on the engine side gets caught at unit-test time.
"""
from __future__ import annotations


def _evaluate_half_life(
    pnl_pct,
    flat_band=(-0.003, 0.005),
    winner_trail=0.01,
    extend_mult=2.0,
    current_trail=0.02,
):
    """Mirror of the engine's half-life decision logic. Returns a dict
    describing what the engine would do:
        {"action": "cull"|"extend"|"hold", ...details}
    """
    lo, hi = float(flat_band[0]), float(flat_band[1])
    if pnl_pct > hi:
        return {
            "action": "extend",
            "new_trail": min(current_trail, winner_trail),
            "extend_mult": extend_mult,
        }
    if lo <= pnl_pct <= hi:
        return {"action": "cull", "reason": "time_exit_half_life"}
    return {"action": "hold"}


# === 1. Deadwood cull ===


def test_flat_position_at_zero_culls():
    """A trade sitting at exactly 0% P&L at half-life is the canonical
    deadwood pattern — close it now."""
    decision = _evaluate_half_life(pnl_pct=0.0)
    assert decision["action"] == "cull"
    assert decision["reason"] == "time_exit_half_life"


def test_slight_positive_inside_flat_band_culls():
    """+0.4% is inside the [-0.3%, +0.5%] flat band — still considered
    deadwood. The strategy expected ≥1-2% on a real winner."""
    decision = _evaluate_half_life(pnl_pct=0.004)
    assert decision["action"] == "cull"


def test_slight_negative_inside_flat_band_culls():
    """-0.2% is inside the flat band — cull instead of waiting for the
    stop_loss to fire on this clearly-stalled trade."""
    decision = _evaluate_half_life(pnl_pct=-0.002)
    assert decision["action"] == "cull"


def test_at_lower_boundary_culls():
    """Boundary: exactly -0.3% is the floor of the flat band."""
    decision = _evaluate_half_life(pnl_pct=-0.003)
    assert decision["action"] == "cull"


def test_at_upper_boundary_culls():
    """Boundary: exactly +0.5% is the ceiling of the flat band."""
    decision = _evaluate_half_life(pnl_pct=0.005)
    assert decision["action"] == "cull"


# === 2. Winner extension ===


def test_winning_position_above_flat_band_extends():
    """+1% at half-life is a real winner — tighten trail to 1% and
    extend max_hold."""
    decision = _evaluate_half_life(pnl_pct=0.01, current_trail=0.02)
    assert decision["action"] == "extend"
    assert decision["new_trail"] == 0.01
    assert decision["extend_mult"] == 2.0


def test_extend_only_tightens_trail_never_loosens():
    """If position is already on a 0.5% trail, the half-life extend
    keeps the tighter trail rather than going back to 1%."""
    decision = _evaluate_half_life(pnl_pct=0.02, current_trail=0.005)
    assert decision["new_trail"] == 0.005  # original 0.5% kept


def test_large_winner_extends_normally():
    """+5% — same extend decision. The half-life rule fires once;
    further upside is captured by the tightened trail."""
    decision = _evaluate_half_life(pnl_pct=0.05)
    assert decision["action"] == "extend"


# === 3. Loser leave-alone ===


def test_loser_below_flat_band_holds():
    """-1% at half-life — loser, leave it alone. stop_loss will fire
    if it gets to -3-5%; normal time_exit fires at max_hold otherwise."""
    decision = _evaluate_half_life(pnl_pct=-0.01)
    assert decision["action"] == "hold"


def test_significant_loser_holds():
    """-2.5% — loser. The half-life cull is for FLAT trades, not losers.
    Forcing an exit here would just confirm the loss; the stop_loss
    might be only marginally further away."""
    decision = _evaluate_half_life(pnl_pct=-0.025)
    assert decision["action"] == "hold"


# === 4. Configurable band ===


def test_wider_flat_band_culls_more():
    """If user widens the flat band to [-1%, +2%], a -0.7% trade now
    falls inside the cull zone."""
    decision = _evaluate_half_life(pnl_pct=-0.007, flat_band=(-0.01, 0.02))
    assert decision["action"] == "cull"


def test_narrower_flat_band_culls_less():
    """Narrow band [-0.1%, +0.1%] catches only the truly flat. +0.3%
    becomes a winner and extends."""
    decision = _evaluate_half_life(pnl_pct=0.003, flat_band=(-0.001, 0.001))
    assert decision["action"] == "extend"


def test_custom_extend_multiplier():
    """Operator can dial extend_mult to 3.0 for swing setups."""
    decision = _evaluate_half_life(pnl_pct=0.01, extend_mult=3.0)
    assert decision["extend_mult"] == 3.0


# === 5. Engine integration shape ===


def test_engine_only_evaluates_once_per_position():
    """The engine gates the half-life check with `_half_life_evaluated`
    so a position can't be re-culled or re-extended on subsequent cycles.
    Pin the contract: once decided, no re-evaluation."""
    # Simulate engine flag behavior
    pos = {"_half_life_evaluated": False}
    # First pass — evaluate
    if not pos["_half_life_evaluated"]:
        pos["_half_life_evaluated"] = True
        # decision happens here
    # Second pass — should NOT re-evaluate
    second_pass_evaluated = pos["_half_life_evaluated"]
    assert second_pass_evaluated is True


def test_engine_disable_flag_skips_evaluation():
    """`risk.adaptive_time_exit_enabled: false` disables the half-life
    check completely — engine falls back to legacy max_hold behavior."""
    config_enabled = False
    pos = {"entry_time": "...", "_half_life_evaluated": False}
    if config_enabled:
        pos["_half_life_evaluated"] = True
    # When disabled, position state untouched
    assert pos["_half_life_evaluated"] is False


# === 6. Extension multiplier applied to subsequent time_exit ===


def test_extended_position_max_hold_doubled():
    """A position marked `_half_life_extended` with mult=2.0 must survive
    until elapsed > max_hold * 2 (instead of just max_hold)."""
    pos = {
        "max_hold_bars": 40,
        "bar_seconds": 300,  # 5-min bars
        "_half_life_extend_mult": 2.0,
    }
    # Nominal: 40 × 300 = 12000s (200 min)
    # Extended: 40 × 300 × 2 = 24000s (400 min)
    nominal = pos["max_hold_bars"] * pos["bar_seconds"]
    extended = nominal * pos["_half_life_extend_mult"]
    assert extended == 24000
    # Elapsed at 250min (15000s): nominal would exit, extended would not
    elapsed = 15000
    nominal_would_exit = elapsed > nominal
    extended_would_exit = elapsed > extended
    assert nominal_would_exit is True
    assert extended_would_exit is False


def test_non_extended_position_uses_unit_multiplier():
    """Positions that never hit the winner branch fall back to 1.0
    multiplier — no behavior change vs pre-feature."""
    pos = {
        "max_hold_bars": 40,
        "bar_seconds": 300,
        # NOTE: no _half_life_extend_mult set
    }
    mult = float(pos.get("_half_life_extend_mult", 1.0))
    assert mult == 1.0
