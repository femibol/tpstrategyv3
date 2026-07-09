"""Per-asset exit engineering + crypto sizing fixes (2026-07 review).

Multi-week review (488 trades, 04-20 → 06-29) found:

  * EQUITY: position-level 38% WR, avgW $11.57 vs avgL $26.96 — needs
    avgW ≥ $44 to break even. Root cause: the global profit-taking
    ladder banked 8%/8%/10% at +0.5%/+1%/+1.5% and the standalone
    break-even armed at +1%, converting designed 2:1 R/R into realized
    0.43:1. 54 partial exits earned +$1.82 avg; 25 full-size stops lost
    -$24.53 avg.
  * CRYPTO: PF 1.53, +$837 over 7 weeks — the SAME ladder works there
    (fast mean-reversion oscillations reward early banking). So the fix
    is per-asset, not global.
  * CRYPTO SIZING: engine.py's tier cap applied the equity PRICE_TIERS
    share table to fractional crypto — JUP-USD sized to $2,000 by the
    sizer, tier-capped to 5,000 units = $1,050. Halved positions on
    sub-$1 coins for no liquidity reason.

Fixes pinned here:
  1. `_pt_targets_for(symbol, pt_config)` — equity gets
     `profit_taking.equity_targets` (first partial +2.5%), crypto keeps
     `targets`, legacy configs (no equity_targets) unchanged.
  2. `_be_trigger_for(symbol, be_cfg)` — equity break-even arms at
     `breakeven.equity_trigger_pct` (2%), crypto keeps `trigger_pct` (1%).
  3. Tier cap exempts crypto (sizer's own crypto path is the limit).
  4. mean_reversion risk cap $100 → $150 (the proven engine sizes up).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

SETTINGS = Path(__file__).parent.parent / "config" / "settings.yaml"
ENGINE = Path(__file__).parent.parent / "bot" / "engine.py"


def _make_engine():
    from bot.engine import TradingEngine
    stub = SimpleNamespace(crypto_suffixes=["-USD", "-USDT", "-BTC", "-ETH"])
    stub._is_crypto_symbol = TradingEngine._is_crypto_symbol.__get__(stub)
    stub._pt_targets_for = TradingEngine._pt_targets_for.__get__(stub)
    stub._be_trigger_for = TradingEngine._be_trigger_for.__get__(stub)
    return stub


PT = {
    "targets": [{"pct_from_entry": 0.005}, {"pct_from_entry": 0.01}],
    "equity_targets": [{"pct_from_entry": 0.025}],
}


def test_equity_gets_equity_targets():
    eng = _make_engine()
    out = eng._pt_targets_for("WYFL", PT)
    assert out == PT["equity_targets"], "equity must use the equity ladder"


def test_crypto_gets_global_targets():
    eng = _make_engine()
    out = eng._pt_targets_for("TRX-USD", PT)
    assert out == PT["targets"], "crypto must keep the original ladder"


def test_legacy_config_without_equity_targets_unchanged():
    eng = _make_engine()
    legacy = {"targets": PT["targets"]}
    assert eng._pt_targets_for("WYFL", legacy) == PT["targets"], (
        "no equity_targets key → equity falls back to global (legacy safe)"
    )


def test_be_trigger_equity_override():
    eng = _make_engine()
    cfg = {"trigger_pct": 0.01, "equity_trigger_pct": 0.02}
    assert eng._be_trigger_for("WYFL", cfg) == 0.02
    assert eng._be_trigger_for("TRX-USD", cfg) == 0.01


def test_be_trigger_fallbacks():
    eng = _make_engine()
    assert eng._be_trigger_for("WYFL", {"trigger_pct": 0.01}) == 0.01
    assert eng._be_trigger_for("WYFL", {}, 0.015) == 0.015


def test_yaml_has_equity_ladder_starting_at_2p5():
    cfg = yaml.safe_load(SETTINGS.read_text())
    pt = cfg["risk"]["profit_taking"]
    eq = pt.get("equity_targets")
    assert eq, "settings.yaml missing profit_taking.equity_targets"
    assert eq[0]["pct_from_entry"] == 0.025, "equity first partial must be +2.5%"
    assert eq[0].get("move_stop") == "breakeven", "first equity tier must arm BE"
    assert all(t["pct_from_entry"] >= 0.025 for t in eq), (
        "no equity tier below +2.5% — sub-2.5% tiers are the winner-choppers"
    )
    # Crypto ladder untouched: still starts at +0.5%
    assert pt["targets"][0]["pct_from_entry"] == 0.005


def test_yaml_equity_be_trigger():
    cfg = yaml.safe_load(SETTINGS.read_text())
    be = cfg["risk"]["breakeven"]
    assert be.get("equity_trigger_pct") == 0.02
    assert be.get("trigger_pct") == 0.01  # crypto unchanged


def test_yaml_mean_reversion_cap_raised():
    cfg = yaml.safe_load(SETTINGS.read_text())
    caps = cfg["risk"]["max_dollar_risk_per_strategy"]
    assert caps["mean_reversion"] == 150, (
        "mean_reversion cap must be 150 — the proven crypto engine sizes up"
    )
    # Unproven strategies stay capped
    assert caps["momentum"] == 50


def test_source_both_ladder_loops_are_per_asset():
    src = ENGINE.read_text()
    assert src.count("self._pt_targets_for(symbol, pt_config)") >= 2, (
        "both partial loops (fast + slow monitor) must resolve targets "
        "per-symbol — one missing re-globalizes the ladder"
    )
    assert src.count("_be_trigger_for(symbol") >= 2, (
        "both break-even checks must use the per-asset trigger"
    )


def test_source_tier_cap_exempts_crypto():
    src = ENGINE.read_text()
    idx = src.find("TIER CAP")
    assert idx > 0
    # The guard must appear shortly before the TIER CAP log line
    window = src[max(0, idx - 700):idx]
    assert "not self._is_crypto_symbol(symbol)" in window, (
        "tier cap must skip crypto — the PRICE_TIERS share table halves "
        "fractional-crypto positions for no liquidity reason (JUP $2,000 → $1,050)"
    )
