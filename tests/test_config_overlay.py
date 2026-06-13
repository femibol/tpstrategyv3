"""Config overlay pattern (Wave 2 audit fix #5, session-10 carry-over).

HANDOFF session 10 surfaced a merge-conflict cascade pattern: the
auto-tuner writes directly to `config/settings.yaml` and
`config/strategies.yaml`, both versioned files. Whenever the host
does `git pull` to deploy a new commit, the tuner's in-flight edits
collide with the incoming change at the YAML byte level, the pull
fails to apply, the bot can't parse the resulting YAML on next
restart, and the system goes into a restart loop until a human
manually resolves with `git checkout --theirs`.

Fix: overlay pattern.

- Base files (`config/settings.yaml`, `config/strategies.yaml`) are
  treated as read-only at runtime. Live edits write to
  `data/auto-tuner-overrides.yaml` and `data/strategy-tuner-overrides.yaml`
  (both gitignored). On load, base + overlay are deep-merged and the
  merged dict becomes `self.settings` / `self.strategies`.
- Auto-tuner + dashboard edits use `Config.save_setting_override(path, value)`
  and `Config.save_strategy_override(strategy, key, value)` exclusively.

These tests pin:
  1. Deep-merge correctness (overlay keys win, missing keys pass through)
  2. Missing overlay = base unchanged
  3. Overlay writes don't touch versioned files
  4. In-memory state updates alongside the overlay write
  5. Allocation overrides survive the renormalization path
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import yaml


def _make_config_in(tmp_dir):
    """Build a Config rooted at a temp dir with synthetic base files
    so the test never touches the real repo."""
    from bot.config import Config

    base = Path(tmp_dir)
    config_dir = base / "config"
    data_dir = base / "data"
    config_dir.mkdir()
    data_dir.mkdir()

    (config_dir / "settings.yaml").write_text(yaml.dump({
        "risk": {"stop_loss_pct": 0.03, "max_positions": 10},
        "capital": {"starting_balance": 5000},
    }))
    (config_dir / "strategies.yaml").write_text(yaml.dump({
        "allocation": {"momentum": 0.2, "mean_reversion": 0.3, "rvol_scalp": 0.5},
        "mean_reversion": {"entry_zscore": -1.5, "rsi_oversold": 32},
    }))

    cfg = Config.__new__(Config)
    cfg.base_dir = base
    cfg.config_dir = config_dir
    cfg._overlay_dir = data_dir
    cfg._settings_overlay_path = data_dir / "auto-tuner-overrides.yaml"
    cfg._strategies_overlay_path = data_dir / "strategy-tuner-overrides.yaml"
    cfg._settings_base = cfg._load_yaml("settings.yaml")
    cfg._strategies_base = cfg._load_yaml("strategies.yaml")
    cfg.settings = cfg._merge_with_overlay(cfg._settings_base, cfg._settings_overlay_path)
    cfg.strategies = cfg._merge_with_overlay(cfg._strategies_base, cfg._strategies_overlay_path)
    cfg.mode = "paper"
    cfg.is_live = False
    cfg.is_paper = True
    return cfg, base


# === 1. Deep merge ===


def test_deep_merge_overlay_keys_win():
    from bot.config import Config
    base = {"risk": {"stop_loss_pct": 0.03, "max_positions": 10}}
    overlay = {"risk": {"stop_loss_pct": 0.05}}
    merged = Config._deep_merge(base, overlay)
    assert merged["risk"]["stop_loss_pct"] == 0.05  # overlay wins
    assert merged["risk"]["max_positions"] == 10    # base preserved


def test_deep_merge_handles_three_levels():
    from bot.config import Config
    base = {"a": {"b": {"c": 1, "d": 2}, "e": 3}}
    overlay = {"a": {"b": {"c": 99}}}
    merged = Config._deep_merge(base, overlay)
    assert merged["a"]["b"]["c"] == 99
    assert merged["a"]["b"]["d"] == 2
    assert merged["a"]["e"] == 3


def test_deep_merge_lists_replace_not_extend():
    """Lists should REPLACE, not extend — semantics match overlays in
    most other config systems and avoid surprising accumulation."""
    from bot.config import Config
    base = {"symbols": ["AAPL", "MSFT"]}
    overlay = {"symbols": ["TSLA"]}
    merged = Config._deep_merge(base, overlay)
    assert merged["symbols"] == ["TSLA"]


def test_deep_merge_empty_overlay_returns_base_unchanged():
    from bot.config import Config
    base = {"a": 1, "b": {"c": 2}}
    merged = Config._deep_merge(base, {})
    assert merged == base


# === 2. Missing overlay ===


def test_missing_overlay_file_falls_back_to_base():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, _ = _make_config_in(tmp)
        # No overlay written yet — settings must equal base
        assert cfg.settings["risk"]["stop_loss_pct"] == 0.03
        assert cfg.strategies["mean_reversion"]["entry_zscore"] == -1.5


def test_corrupt_overlay_file_falls_back_to_base():
    """A malformed overlay must not crash the bot at startup —
    silently fall back to base. The auto-tuner can re-write on its
    next cycle."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, base = _make_config_in(tmp)
        (base / "data" / "auto-tuner-overrides.yaml").write_text("not: : valid: yaml: [[[")
        merged = cfg._merge_with_overlay(cfg._settings_base, cfg._settings_overlay_path)
        assert merged["risk"]["stop_loss_pct"] == 0.03


# === 3. Override writes ===


def test_save_setting_override_writes_to_overlay_only():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, base = _make_config_in(tmp)
        cfg.save_setting_override("risk.stop_loss_pct", 0.05)
        # Overlay file exists with just the change
        overlay = yaml.safe_load((base / "data" / "auto-tuner-overrides.yaml").read_text())
        assert overlay == {"risk": {"stop_loss_pct": 0.05}}
        # Versioned file UNTOUCHED
        versioned = yaml.safe_load((base / "config" / "settings.yaml").read_text())
        assert versioned["risk"]["stop_loss_pct"] == 0.03


def test_save_setting_override_updates_in_memory():
    """The change must be live without restart."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, _ = _make_config_in(tmp)
        cfg.save_setting_override("risk.stop_loss_pct", 0.05)
        assert cfg.settings["risk"]["stop_loss_pct"] == 0.05


def test_save_strategy_override_isolated_file():
    """Strategy overrides go to a separate file so they can be wiped
    independently of risk-config tuner edits."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, base = _make_config_in(tmp)
        cfg.save_strategy_override("mean_reversion", "entry_zscore", -1.0)
        s_overlay = yaml.safe_load((base / "data" / "strategy-tuner-overrides.yaml").read_text())
        assert s_overlay == {"mean_reversion": {"entry_zscore": -1.0}}
        # Settings overlay untouched
        assert not (base / "data" / "auto-tuner-overrides.yaml").exists()


def test_override_persists_across_reload():
    """The whole point: after a bot restart (= new Config instance),
    overlays must still apply on top of base."""
    from bot.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg, base = _make_config_in(tmp)
        cfg.save_setting_override("risk.stop_loss_pct", 0.06)
        # Simulate restart: fresh Config instance pointing at the same tree
        cfg2 = Config.__new__(Config)
        cfg2.base_dir = base
        cfg2.config_dir = base / "config"
        cfg2._overlay_dir = base / "data"
        cfg2._settings_overlay_path = base / "data" / "auto-tuner-overrides.yaml"
        cfg2._strategies_overlay_path = base / "data" / "strategy-tuner-overrides.yaml"
        cfg2._settings_base = cfg2._load_yaml("settings.yaml")
        cfg2._strategies_base = cfg2._load_yaml("strategies.yaml")
        cfg2.settings = cfg2._merge_with_overlay(cfg2._settings_base, cfg2._settings_overlay_path)
        cfg2.strategies = cfg2._merge_with_overlay(cfg2._strategies_base, cfg2._strategies_overlay_path)
        assert cfg2.settings["risk"]["stop_loss_pct"] == 0.06


# === 4. Multiple overrides accumulate ===


def test_multiple_overrides_accumulate_in_overlay():
    """Two consecutive overrides on different paths must produce a
    merged overlay file (not one overwriting the other)."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, base = _make_config_in(tmp)
        cfg.save_setting_override("risk.stop_loss_pct", 0.05)
        cfg.save_setting_override("risk.max_positions", 15)
        overlay = yaml.safe_load((base / "data" / "auto-tuner-overrides.yaml").read_text())
        assert overlay == {"risk": {"stop_loss_pct": 0.05, "max_positions": 15}}


def test_repeated_override_same_path_uses_latest():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, base = _make_config_in(tmp)
        cfg.save_setting_override("risk.stop_loss_pct", 0.05)
        cfg.save_setting_override("risk.stop_loss_pct", 0.07)
        overlay = yaml.safe_load((base / "data" / "auto-tuner-overrides.yaml").read_text())
        assert overlay["risk"]["stop_loss_pct"] == 0.07


# === 5. Allocation normalization through overlay ===


def test_allocation_override_persists_through_overlay():
    """The auto-tuner's `_normalize_allocations` writes each
    allocation back through `save_strategy_override` — verify the
    overlay carries the renormalized values."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, base = _make_config_in(tmp)
        cfg.save_strategy_override("allocation", "momentum", 0.3)
        cfg.save_strategy_override("allocation", "mean_reversion", 0.7)
        overlay = yaml.safe_load((base / "data" / "strategy-tuner-overrides.yaml").read_text())
        assert overlay["allocation"]["momentum"] == 0.3
        assert overlay["allocation"]["mean_reversion"] == 0.7
        # Versioned strategies.yaml untouched
        versioned = yaml.safe_load((base / "config" / "strategies.yaml").read_text())
        assert versioned["allocation"]["momentum"] == 0.2


# === 6. Anti-regression: legacy code path locked out ===


def test_auto_tuner_does_not_call_save_settings_directly():
    """Pin the refactor: auto_tuner.py must NOT call save_settings()
    or _save_strategies_yaml() in the apply path anymore. If it does,
    we're back in the merge-conflict cascade scenario."""
    src = (Path(__file__).parent.parent / "bot" / "learning" / "auto_tuner.py").read_text()
    # The full apply path
    apply_section = src.split("def _apply_param")[1].split("def _normalize_allocations")[0]
    assert "save_settings()" not in apply_section, (
        "_apply_param must not call self.config.save_settings() — "
        "use save_setting_override() instead"
    )
    # Also confirm the bulk-write at the end of run_auto_tune was removed
    run_section = src.split("def run_auto_tune")[1].split("def _get_current_params")[0]
    assert "self.config.save_settings()" not in run_section
    assert "self._save_strategies_yaml()" not in run_section
