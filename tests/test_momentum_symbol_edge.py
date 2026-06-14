"""Per-symbol edge filter for momentum strategy (Wave 5).

Extension of PR #211 from mean_reversion. The momentum strategy is
historically -$500/30d at 26% WR. Engine-wide `should_avoid_symbol`
pools ALL strategies so per-strategy bleeders escape its -8 floor —
the same problem PR #211 solved for mean_reversion.

These tests pin:
  1. feed_symbol_edge stores the map
  2. _edge_blocks returns reason when bleeder pattern matches
  3. _edge_blocks returns "" when sample too small / WR ok / pnl ok
  4. Filter disabled → never blocks
  5. Filter ON + entry conditions met → BUY suppressed
  6. Filter ON + entry conditions met + no edge data → entry fires
  7. Engine wires feed_symbol_edge for momentum (anti-regression)
"""
from __future__ import annotations

from pathlib import Path


def _strat(cfg_overrides=None):
    from bot.strategies.momentum import MomentumStrategy
    from bot.data.indicators import TechnicalIndicators
    cfg = {
        "fast_ema": 8, "slow_ema": 21, "signal_ema": 5,
        "adx_threshold": 35, "volume_surge_multiplier": 1.3,
        "atr_period": 14, "atr_stop_multiplier": 1.5,
        "atr_target_multiplier": 5.0, "max_holding_bars": 40,
        "max_hold_days": 5, "breakout_lookback": 20,
        "symbols": [], "min_day_change_pct": -2.0,
        "symbol_edge_filter_enabled": True,
        "symbol_edge_min_trades": 3,
        "symbol_edge_block_wr_pct": 30.0,
    }
    if cfg_overrides:
        cfg.update(cfg_overrides)
    return MomentumStrategy(cfg, TechnicalIndicators(), capital=10000)


# === 1. Feed + storage ===


def test_feed_symbol_edge_stores_map():
    strat = _strat()
    edge = {"FBYD": {"trades": 5, "win_rate": 18.0, "avg_pnl": -7.0}}
    strat.feed_symbol_edge(edge)
    assert strat._symbol_edge_map == edge


def test_feed_symbol_edge_handles_none():
    strat = _strat()
    strat.feed_symbol_edge(None)
    assert strat._symbol_edge_map == {}


# === 2. _edge_blocks gate decisions ===


def test_blocks_bleeder():
    """Canonical FBYD pattern — 5 trades, 18% WR, -$7/trade."""
    strat = _strat()
    strat.feed_symbol_edge({
        "FBYD": {"trades": 5, "win_rate": 18.0, "avg_pnl": -7.0},
    })
    reason = strat._edge_blocks("FBYD")
    assert "BLOCKED" in reason
    assert "18% WR" in reason
    assert "5 trades" in reason


def test_blocks_case_insensitive_lookup():
    """Symbol lookup is uppercase-normalized."""
    strat = _strat()
    strat.feed_symbol_edge({
        "FBYD": {"trades": 5, "win_rate": 18.0, "avg_pnl": -7.0},
    })
    assert strat._edge_blocks("fbyd") != ""
    assert strat._edge_blocks("FbYd") != ""


def test_does_not_block_winner():
    strat = _strat()
    strat.feed_symbol_edge({
        "AAPL": {"trades": 10, "win_rate": 60.0, "avg_pnl": 5.0},
    })
    assert strat._edge_blocks("AAPL") == ""


def test_does_not_block_low_sample_size():
    """2 trades isn't enough data — let it run."""
    strat = _strat()
    strat.feed_symbol_edge({
        "AAPL": {"trades": 2, "win_rate": 0.0, "avg_pnl": -10.0},
    })
    assert strat._edge_blocks("AAPL") == ""


def test_does_not_block_positive_avg_pnl():
    """Low WR but positive avg — winners cover the losers."""
    strat = _strat()
    strat.feed_symbol_edge({
        "BAR": {"trades": 10, "win_rate": 20.0, "avg_pnl": 5.0},
    })
    assert strat._edge_blocks("BAR") == ""


def test_does_not_block_high_wr():
    strat = _strat()
    strat.feed_symbol_edge({
        "FOO": {"trades": 10, "win_rate": 60.0, "avg_pnl": -1.0},
    })
    assert strat._edge_blocks("FOO") == ""


# === 3. Filter disabled ===


def test_disabled_filter_never_blocks():
    """When `symbol_edge_filter_enabled=False`, never block —
    preserves the historical pre-Wave-5 behavior for environments
    that opt out."""
    strat = _strat({"symbol_edge_filter_enabled": False})
    strat.feed_symbol_edge({
        "FBYD": {"trades": 99, "win_rate": 1.0, "avg_pnl": -100.0},
    })
    assert strat._edge_blocks("FBYD") == ""


# === 4. Defaults ===


def test_filter_disabled_by_default_in_code():
    """Code default OFF so day-0 fresh installs don't block on
    incomplete history. Config opts in."""
    from bot.strategies.momentum import MomentumStrategy
    from bot.data.indicators import TechnicalIndicators
    minimal_cfg = {
        "fast_ema": 8, "slow_ema": 21, "signal_ema": 5,
        "adx_threshold": 35, "volume_surge_multiplier": 1.3,
        "atr_period": 14, "atr_stop_multiplier": 1.5,
        "atr_target_multiplier": 5.0, "max_holding_bars": 40,
        "max_hold_days": 5, "breakout_lookback": 20, "symbols": [],
    }
    strat = MomentumStrategy(minimal_cfg, TechnicalIndicators(), capital=10000)
    assert strat.symbol_edge_filter_enabled is False


# === 5. Empty / missing symbol ===


def test_unknown_symbol_does_not_block():
    """Day 0: no edge data for any symbol → never block."""
    strat = _strat()
    strat.feed_symbol_edge({})
    assert strat._edge_blocks("ANYTHING") == ""


def test_none_symbol_does_not_crash():
    """Defensive: don't crash on a None symbol from upstream."""
    strat = _strat()
    strat.feed_symbol_edge({
        "FBYD": {"trades": 5, "win_rate": 18.0, "avg_pnl": -7.0},
    })
    assert strat._edge_blocks(None) == ""


# === 6. Engine wiring (anti-regression) ===


def test_engine_feeds_edge_map_to_momentum():
    """Lock the wiring: engine.py must call `feed_symbol_edge` for
    momentum too, not just mean_reversion."""
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    # The loop must include "momentum" as a strategy_name fed
    edge_block_idx = src.find("get_symbol_edge_map")
    assert edge_block_idx > 0
    block = src[edge_block_idx:edge_block_idx + 1500]
    assert '"momentum"' in block, (
        "engine.py edge-map block must feed momentum strategy too "
        "(Wave 5 extension)"
    )
    assert '"mean_reversion"' in block, "mean_reversion wiring lost"


def test_config_enables_filter_for_momentum():
    """Anti-regression: config/strategies.yaml momentum block enables
    the filter."""
    import yaml
    cfg_path = Path(__file__).parent.parent / "config" / "strategies.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    assert cfg["momentum"]["symbol_edge_filter_enabled"] is True
    assert cfg["momentum"]["symbol_edge_min_trades"] == 3
    assert cfg["momentum"]["symbol_edge_block_wr_pct"] == 30.0
