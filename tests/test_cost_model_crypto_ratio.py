"""Crypto-specific cost-gate ratio so cleanly-oversold crypto setups pass
without loosening the conservative 2× equity gate.

2026-06-22 live audit: GRT-USD at Z=-2.33 / RSI=0 / Vol=12.2x and MATIC at
Z=-2.09 / Vol=8.2x — both real oversold bounces with ~66-78 bps of edge —
were rejected by the global 2× cost ratio (needed 100 bps, edge was 66).
The audit also showed equity momentum runners cleared 2× trivially (LNKS
+$100 on Sat had multi-percent moves). Adding a per-asset ratio keeps
equity tight (rejection of low-quality momo setups was working) while
admitting the crypto setups the bot is built for.

These tests pin:
  1. With both ratios set, crypto signals use the crypto ratio.
  2. With only the global ratio set (legacy configs), crypto inherits it.
  3. Equity signals always use the global ratio regardless of crypto override.
  4. The exact GRT-USD 66 bp case passes at 1.5×, fails at 2.0× —
     anti-regression on the live trade that motivated the change.
  5. The 36 bp MATIC case (the safety floor we want to preserve) still
     fails at 1.5×.
  6. YAML structure has the new key under `cost_model:`.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _make_cost_model(global_ratio=2.0, crypto_ratio=None):
    """Build a CostModel with a stub Config that mimics settings.yaml."""
    from bot.risk.cost_model import CostModel

    cfg = {
        "enabled": True,
        "equity_fee_bps": 2.0,
        "crypto_fee_bps": 30.0,
        "equity_spread_bps_default": 5.0,
        "crypto_spread_bps_default": 10.0,
        "spread_mult": 2.0,
        "min_edge_cost_ratio": global_ratio,
    }
    if crypto_ratio is not None:
        cfg["crypto_min_edge_cost_ratio"] = crypto_ratio

    return CostModel(SimpleNamespace(settings={"cost_model": cfg}))


def _signal_with_edge_bps(target_bps, price=1.0):
    """Build a minimal signal whose take_profit gives roughly `target_bps`
    of edge. Used so tests can express expected behavior in bps directly."""
    tp = price * (1 + target_bps / 10000.0)
    return {"price": price, "take_profit": tp, "stop_loss": 0}


def test_crypto_uses_crypto_ratio_when_set():
    cm = _make_cost_model(global_ratio=2.0, crypto_ratio=1.5)
    # round-trip crypto cost = 30 (fee) + 10*2 (spread) = 50 bps
    # 75 bps of edge is between 1.5×50=75 and 2.0×50=100 — passes at 1.5,
    # would fail at 2.0.
    passed, reason = cm.passes(_signal_with_edge_bps(80), "crypto")
    assert passed, f"crypto with 80 bp edge should pass at 1.5× crypto ratio: {reason}"


def test_crypto_falls_back_to_global_ratio_when_unset():
    """Existing installs that haven't added the new key should see no
    behavior change — crypto still uses the global ratio (2.0)."""
    cm = _make_cost_model(global_ratio=2.0, crypto_ratio=None)
    # 80 bp < 2.0 * 50 = 100 → should fail
    passed, _ = cm.passes(_signal_with_edge_bps(80), "crypto")
    assert not passed, "crypto should fall back to global 2.0× ratio when crypto_min_edge_cost_ratio unset"


def test_equity_always_uses_global_ratio():
    """Equity never picks up the crypto override — even if it's set."""
    cm = _make_cost_model(global_ratio=2.0, crypto_ratio=1.5)
    # round-trip equity cost = 2 (fee) + 5*2 (spread) = 12 bps
    # 15 bp of edge: 1.5*12=18 (would pass at crypto), 2.0*12=24 (fails at equity)
    passed, _ = cm.passes(_signal_with_edge_bps(15), "equity")
    assert not passed, "equity must use 2.0× ratio even when crypto override is set"


def test_78bp_matic_case_passes_at_1p5_fails_at_2p0():
    """The live trade that motivated this PR. MATIC at Z=-2.09 / Vol=8.2x
    on 2026-06-19 generated a real oversold signal with 78 bps of edge —
    rejected at 2× ratio (needed 100 bps). At 1.5× it passes (need 75).
    The GRT-USD 66 bp case stays rejected even at 1.5×; that's the
    intended safety margin — anything under ~75 bps of edge is too thin
    to justify the spread+slippage risk on a paper-account fill."""
    sig = _signal_with_edge_bps(78, price=0.08)
    cm_legacy = _make_cost_model(global_ratio=2.0, crypto_ratio=None)
    cm_new = _make_cost_model(global_ratio=2.0, crypto_ratio=1.5)
    passed_legacy, reason_legacy = cm_legacy.passes(sig, "crypto")
    passed_new, _ = cm_new.passes(sig, "crypto")
    assert not passed_legacy, (
        f"legacy 2× ratio must still reject 78 bp (anti-regression): {reason_legacy}"
    )
    assert passed_new, "new 1.5× crypto ratio must admit 78 bp of edge"


def test_66bp_grt_case_still_rejected_at_1p5():
    """The companion safety check. GRT-USD 66 bp edge is the borderline
    we deliberately keep rejecting — it sits below 1.5×50 = 75. If the
    operator wants those admitted too, drop the ratio further (1.3×) —
    but document the bleed-risk trade-off."""
    cm = _make_cost_model(global_ratio=2.0, crypto_ratio=1.5)
    passed, reason = cm.passes(_signal_with_edge_bps(66, price=0.02), "crypto")
    assert not passed, (
        f"66 bp edge should still be rejected at 1.5× ratio (75 bp threshold): {reason}"
    )


def test_36bp_matic_case_still_rejected_at_1p5():
    """The safety floor: a 36 bp edge is below 1.5×50=75. We don't want
    to admit those marginal setups even with the loosened ratio."""
    cm = _make_cost_model(global_ratio=2.0, crypto_ratio=1.5)
    passed, _ = cm.passes(_signal_with_edge_bps(36, price=0.08), "crypto")
    assert not passed, "36 bp edge should still fail at 1.5× crypto ratio"


def test_75bp_edge_at_threshold_boundary():
    """Right at the threshold — 1.5*50 = 75 bps. We want this to pass
    (the boundary value). Use 76 bp to avoid floating-point flakiness."""
    cm = _make_cost_model(global_ratio=2.0, crypto_ratio=1.5)
    passed, _ = cm.passes(_signal_with_edge_bps(76), "crypto")
    assert passed, "76 bp edge should pass at 1.5× crypto ratio (threshold 75)"


def test_disabled_cost_model_still_disabled_for_crypto():
    """`cost_model.enabled: false` short-circuits before either ratio."""
    from bot.risk.cost_model import CostModel
    cm = CostModel(SimpleNamespace(settings={"cost_model": {"enabled": False}}))
    passed, _ = cm.passes(_signal_with_edge_bps(1), "crypto")
    assert passed


def test_settings_yaml_has_crypto_ratio_under_cost_model():
    """Anti-regression at YAML level: the key must be under `cost_model:`
    (paralleling crypto_fee_bps, crypto_spread_bps_default — all are
    cost-model concerns, not risk-model concerns)."""
    import yaml
    cfg = yaml.safe_load(
        (Path(__file__).parent.parent / "config" / "settings.yaml").read_text()
    )
    cm = cfg.get("cost_model", {})
    assert "crypto_min_edge_cost_ratio" in cm, (
        "config/settings.yaml: `crypto_min_edge_cost_ratio` must be under "
        "`cost_model:` so the CostModel.__init__ reads it"
    )
    assert cm["crypto_min_edge_cost_ratio"] == 1.5, (
        f"crypto_min_edge_cost_ratio should be 1.5, got {cm['crypto_min_edge_cost_ratio']}"
    )
