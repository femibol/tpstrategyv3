"""Auto-tuner zero-fill guard — never reward an untraded strategy with capital.

2026-07-22 live finding: `daily_trend_rider` took ZERO trades for weeks (its
daily-scan gate qualified no candidates), yet the AI auto-tuner kept *raising*
its allocation — 0.18 → 0.20 → 0.22 across a single session — because a
strategy with no fills has no losses for the AI to penalize. 22% of capital
was pinned to a strategy that never traded, starving the ones that work.

Fix (bot/learning/auto_tuner.py run_auto_tune): build the set of strategies
that actually filled a trade; for any `alloc_*` param whose strategy is absent,
override the AI's suggestion toward that param's floor. Combined with the
existing step-limiter this DECAYS a dead strategy's allocation toward its
minimum and can never grow it. The guard lifts itself the moment the strategy
starts trading again.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from bot.learning.auto_tuner import AutoTuner, PARAM_BOUNDS

STRATEGIES = Path(__file__).parent.parent / "config" / "strategies.yaml"


class _FakeConfig:
    """Minimal Config surface used by AutoTuner.run_auto_tune."""

    def __init__(self, base_dir, allocation):
        self.base_dir = base_dir
        self.config_dir = base_dir
        self.settings = {"risk": {}}
        self.strategies = {
            "allocation": dict(allocation),
            # blocks _get_current_params reads via .get() — empty is fine
            "mean_reversion": {}, "momentum": {}, "rvol_momentum": {},
            "smc_forever": {}, "rvol_scalp": {}, "vwap_scalp": {},
            "pairs_trading": {}, "daily_trend_rider": {},
        }

    def save_setting_override(self, path, value):  # pragma: no cover - unused
        pass

    def save_strategy_override(self, section, key, value):  # pragma: no cover
        pass


def _run_with_ai(tmp_path, allocation, ai_recs, trade_history):
    """Drive run_auto_tune with a stubbed AI response; capture applied values."""
    cfg = _FakeConfig(tmp_path, allocation)
    tuner = AutoTuner(cfg, data_dir=tmp_path)
    tuner.api_key = "test-key"          # bypass the no-key early return
    tuner._last_tune_time = 0           # bypass the rate limiter

    applied: dict[str, float] = {}
    tuner._get_ai_recommendations = lambda *a, **k: dict(ai_recs)
    tuner._apply_param = lambda param_key, value: applied.__setitem__(param_key, value)
    tuner._save_changelog = lambda *a, **k: None
    tuner._normalize_allocations = lambda: None

    tuner.run_auto_tune(trade_history, performance_stats={}, strategy_scores={})
    return applied


def _history(strategy, n=20):
    return [{"strategy": strategy, "pnl": 1.0} for _ in range(n)]


def test_untraded_strategy_allocation_cannot_grow(tmp_path):
    """AI wants to *raise* an untraded strategy — guard decays it toward floor."""
    applied = _run_with_ai(
        tmp_path,
        allocation={"daily_trend_rider": 0.22, "momentum": 0.20},
        ai_recs={"alloc_daily_trend_rider": 0.30},   # AI says: go UP
        trade_history=_history("momentum"),          # trend_rider never traded
    )
    floor = PARAM_BOUNDS["alloc_daily_trend_rider"][0]
    assert "alloc_daily_trend_rider" in applied, "guard must still apply a (downward) change"
    # Pinned toward floor, then step-limited from 0.22 → strictly DOWN, never up.
    assert applied["alloc_daily_trend_rider"] < 0.22
    assert applied["alloc_daily_trend_rider"] >= floor


def test_traded_strategy_allocation_still_grows(tmp_path):
    """A strategy that HAS traded is untouched by the guard — normal tuning."""
    applied = _run_with_ai(
        tmp_path,
        allocation={"daily_trend_rider": 0.22, "momentum": 0.20},
        ai_recs={"alloc_momentum": 0.30},            # AI says: go UP
        trade_history=_history("momentum"),          # momentum DID trade
    )
    # Step-limited increase (0.20 + 0.05 max_step), i.e. it grew as suggested.
    assert applied["alloc_momentum"] > 0.20


def test_trend_rider_gate_loosened_to_produce_candidates():
    """Config #2: the zero-candidate gate is loosened so the strategy can fire."""
    cfg = yaml.safe_load(STRATEGIES.read_text())
    tr = cfg["daily_trend_rider"]
    assert tr["min_green_days"] == 2, "min_green_days must be loosened 3→2"
    assert tr["adx_threshold"] == 15, "adx_threshold must be loosened 20→15"
