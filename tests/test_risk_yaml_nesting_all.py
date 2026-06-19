"""All risk-conceptual keys must be nested under `risk:`, not `cost_model:`.

2026-06-18 audit: while fixing `blocked_symbols` (which had bled $134 across
three leveraged-ETF trades because it was mis-nested), found that 12 more
risk-conceptual keys had the same problem. The engine reads
`risk.X` for every one of them — but they were nested under `cost_model:`
and the `.get(X, default)` calls fell back to hardcoded defaults silently.

Three of those drifts mattered:

  * `strategy_daily_dd_pause_pct`: code default 0.03 vs YAML 0.05.
    Strategies were pausing 2% earlier than the post-RKLB tuning intended.

  * `min_volume`: code default 50000 vs YAML 200000. Universe was 4x looser
    than the YAML claimed — thin-volume names slipped through risk gates.

  * `profit_taking`: code defaults `pt_enabled=False` with empty targets vs
    YAML 10-tier ladder. The configured intra-candle partials NEVER fired
    from the gated paths (only velocity_exits / momentum_reversal partials,
    which use different gates). Tier 1-10 partials were silently disabled.

The rest matched defaults so the bug was invisible per-key, but the YAML
was lying about which values were active.

These tests pin every moved key at two levels:
  1. YAML structure: each key is under `risk:` and NOT under `cost_model:`.
  2. Runtime: `Config().risk_config[X]` returns the expected YAML value.

Cost-model-only keys (`equity_fee_bps`, etc.) stay where they are — those
ARE cost-model concerns. The test also asserts cost_model still has
those keys so an over-eager future refactor doesn't move them too.
"""
from __future__ import annotations

from pathlib import Path

import yaml


SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"

# Keys that MUST be under `risk:` because engine reads `risk.X`.
# Each entry: (key, expected_yaml_value_or_None_for_complex)
RISK_KEYS = [
    ("max_total_trades_per_day", 25),
    ("max_total_crypto_trades_per_day", 50),
    ("strategy_daily_dd_pause_pct", 0.05),
    ("premarket_news_check", True),
    ("min_volume", 200000),
    ("min_price", 0.50),
    ("scanner_max_price", 500.00),
    ("falling_knife_pct", -5.0),
    ("blocked_symbols", None),   # check non-empty list below
    ("profit_taking", None),     # complex dict, check sub-keys below
    ("velocity_exits", None),
    ("breakeven", None),
    ("portfolio_limits", None),
]

# Keys that MUST stay under `cost_model:` — these are real cost-model.
COST_MODEL_KEYS = (
    "enabled", "equity_fee_bps", "crypto_fee_bps",
    "equity_spread_bps_default", "crypto_spread_bps_default",
    "spread_mult", "min_edge_cost_ratio",
)


def _load():
    return yaml.safe_load(SETTINGS_PATH.read_text())


def test_all_risk_keys_under_risk_not_cost_model():
    cfg = _load()
    risk = cfg.get("risk", {})
    cm = cfg.get("cost_model", {})
    for key, _ in RISK_KEYS:
        assert key in risk, (
            f"settings.yaml: `{key}` must be under `risk:` so the engine's "
            f"`risk_config.get(...)` finds it. Currently missing."
        )
        assert key not in cm, (
            f"settings.yaml: `{key}` is mis-nested under `cost_model:`. "
            f"Engine reads `risk.{key}` and falls back to a code default; "
            f"the YAML value is invisible. Move it back under `risk:`."
        )


def test_risk_keys_have_expected_values_at_runtime():
    """Production code path: `Config().risk_config[X]` returns the YAML
    value, not a silent code-default fallback."""
    from bot.config import Config
    cfg = Config().risk_config
    for key, expected in RISK_KEYS:
        if expected is None:
            continue   # complex keys handled separately
        actual = cfg.get(key)
        assert actual == expected, (
            f"risk_config[{key!r}] == {actual!r}, expected {expected!r}. "
            f"Either the YAML value drifted or the key isn't being read."
        )


def test_blocked_symbols_is_populated_at_runtime():
    """The specific bug that bled $134 (SOXL/JNUG/TZA) — verified separately
    in test_blocked_symbols_yaml_nesting.py but also pinned here to keep
    this audit comprehensive."""
    from bot.config import Config
    blocked = Config().risk_config.get("blocked_symbols", [])
    blocked_upper = {s.upper() for s in blocked}
    assert len(blocked) > 30
    for must_block in ("SOXL", "JNUG", "TZA", "TQQQ", "HIBS"):
        assert must_block in blocked_upper, f"{must_block} must be blocked"


def test_profit_taking_block_is_active_at_runtime():
    """The 10-tier ladder was silently disabled (pt_enabled defaulted to
    False). Activating it is a deliberate behavior change in this PR —
    test pins the activation so we notice if the block ever goes dark
    again."""
    from bot.config import Config
    pt = Config().risk_config.get("profit_taking", {})
    assert pt.get("enabled") is True, "profit_taking must be enabled"
    targets = pt.get("targets", [])
    assert len(targets) >= 9, (
        f"profit_taking.targets has only {len(targets)} entries — "
        f"the YAML defines a 10-tier ladder. Either the YAML was edited "
        f"or the merge dropped tiers."
    )
    # First two tiers are the early scalps — pin them.
    assert targets[0].get("pct_from_entry") == 0.005
    assert targets[1].get("pct_from_entry") == 0.01


def test_velocity_exits_block_is_present_at_runtime():
    from bot.config import Config
    vel = Config().risk_config.get("velocity_exits", {})
    assert vel.get("enabled") is True
    assert vel.get("fast_spike_pct") == 0.015
    assert vel.get("fast_spike_window_sec") == 45
    assert vel.get("reversal_retrace_pct") == 0.30


def test_breakeven_block_is_present_at_runtime():
    from bot.config import Config
    be = Config().risk_config.get("breakeven", {})
    assert be.get("enabled") is True
    assert be.get("trigger_pct") == 0.01
    assert be.get("buffer_pct") == 0.005


def test_portfolio_limits_block_is_present_at_runtime():
    from bot.config import Config
    pl = Config().risk_config.get("portfolio_limits", {})
    assert pl.get("max_single_name_pct") == 0.25
    assert pl.get("max_gross_exposure_pct") == 1.50
    assert pl.get("max_loss_per_position_pct") == 0.08


def test_cost_model_keeps_its_actual_keys():
    """Don't drag cost-model concerns over to risk: by accident."""
    cfg = _load()
    cm = cfg.get("cost_model", {})
    for k in COST_MODEL_KEYS:
        assert k in cm, (
            f"settings.yaml: `cost_model.{k}` is missing — over-eager "
            f"refactor moved real cost-model keys to risk: too. Move back."
        )


def test_cost_model_does_not_have_risk_keys():
    cfg = _load()
    cm = cfg.get("cost_model", {})
    risk_only_keys_under_cost_model = [
        k for k, _ in RISK_KEYS if k in cm
    ]
    assert not risk_only_keys_under_cost_model, (
        f"cost_model: still holds risk-conceptual keys: "
        f"{risk_only_keys_under_cost_model}. Move them to risk:."
    )
