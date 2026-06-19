"""`risk.blocked_symbols` must actually be readable as `risk.X` — the
list lived for an unknown duration under `cost_model:` due to an
indentation slip and was invisible to the engine at runtime.

2026-06-18 audit: SOXL traded -$65 today, JNUG -$46 on 2026-06-15, TZA
-$22 on 2026-05-18. All three are in the `blocked_symbols` list. None
were blocked. Root cause: `_execute_signal` (engine.py:5918) reads
`self.config.risk_config.get("blocked_symbols", [])` — and
`risk_config` is `settings["risk"]` — but the YAML had the list nested
under `cost_model:`. The `.get(..., [])` fell back to `[]` silently;
every leveraged ETF flowed through.

These tests pin two guarantees:
  1. `Config().risk_config["blocked_symbols"]` is non-empty and contains
     all the canonical leveraged-ETF tickers we've explicitly blocked.
  2. The YAML structure has `blocked_symbols` under `risk:`, NOT under
     `cost_model:`. A future refactor that re-indents this block back
     where it doesn't belong silently re-opens the same hole.
"""
from __future__ import annotations

from pathlib import Path


def test_runtime_risk_config_has_blocked_symbols():
    """Production code path: `Config().risk_config` must surface the
    blocked list. This is what `_execute_signal` checks at line 5918."""
    from bot.config import Config
    cfg = Config()
    blocked = cfg.risk_config.get("blocked_symbols", [])
    assert len(blocked) > 30, (
        f"blocked_symbols at runtime has only {len(blocked)} entries — "
        f"the YAML lists 50+. Likely re-mis-nested under cost_model: again."
    )
    blocked_upper = {s.upper() for s in blocked}
    # The three tickers that bled real money before the fix.
    assert "SOXL" in blocked_upper, "SOXL must be blocked (bled -$65 on 2026-06-18)"
    assert "JNUG" in blocked_upper, "JNUG must be blocked (bled -$46 on 2026-06-15)"
    assert "TZA" in blocked_upper, "TZA must be blocked (bled -$22 on 2026-05-18)"
    # Other canonical 3x ETFs.
    for sym in ("TQQQ", "SQQQ", "SOXS", "SPXS", "HIBS", "HIBL"):
        assert sym in blocked_upper, f"{sym} must be in blocked_symbols"


def test_yaml_structure_blocked_symbols_under_risk_not_cost_model():
    """Anti-regression at YAML level. The bug was a sibling-indentation
    error: someone added knobs into `cost_model:` thinking they were
    extending `risk:`, and the next reader followed the pattern. This
    test fails fast if the list ever drifts back."""
    import yaml
    p = Path(__file__).parent.parent / "config" / "settings.yaml"
    cfg = yaml.safe_load(p.read_text())
    assert "blocked_symbols" in cfg.get("risk", {}), (
        "config/settings.yaml: `blocked_symbols` must be under `risk:` so "
        "the engine's `risk_config.get(...)` actually finds it"
    )
    assert "blocked_symbols" not in cfg.get("cost_model", {}), (
        "config/settings.yaml: `blocked_symbols` is mis-nested under "
        "`cost_model:`. Engine reads `risk.blocked_symbols` and falls back "
        "to []; every leveraged ETF on the 'block list' will trade through."
    )


def test_blocked_symbols_in_settings_yaml_has_canonical_entries():
    """Source-of-truth check on the YAML file itself."""
    import yaml
    p = Path(__file__).parent.parent / "config" / "settings.yaml"
    cfg = yaml.safe_load(p.read_text())
    blocked = cfg.get("risk", {}).get("blocked_symbols", [])
    blocked_upper = {s.upper() for s in blocked}
    # If you add new tickers, append here so the test catches accidental deletions.
    canonical = {"SOXL", "TQQQ", "SOXS", "SQQQ", "TZA", "TNA", "HIBS", "HIBL",
                 "JNUG", "NUGT", "FAZ", "UVXY", "TSLL", "NVDL", "LABU", "LABD"}
    missing = canonical - blocked_upper
    assert not missing, (
        f"Canonical leveraged-ETF tickers missing from risk.blocked_symbols: "
        f"{sorted(missing)}. Don't remove these unless you know why — each "
        f"was added after a real loss."
    )
