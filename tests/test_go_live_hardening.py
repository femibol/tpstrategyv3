"""Go-live hardening (2026-07-10 audit): 7 fixes pinned.

Audit findings: (1) live mode inherits paper workarounds (MARKET brackets,
IEX routing) with no guard; (2) $25K boot-balance fallback would mis-size a
live book; (3) capital.max_portfolio_risk was dead config, read nowhere;
(4) all account guards are IBKR-only — crypto (separate TradersPost account)
invisible; (5) three self.positions mutation sites unlocked (race vs close);
(6) momentum_runner emitted 0-12 scores into a 0-100 gate — 30% allocation
structurally dead; (7) momentum's config volume floor never gated entries.
"""
from __future__ import annotations
from pathlib import Path
import yaml

ROOT = Path(__file__).parent.parent
ENGINE = (ROOT / "bot" / "engine.py").read_text()
MAIN = (ROOT / "bot" / "main.py").read_text()
MANAGER = (ROOT / "bot" / "risk" / "manager.py").read_text()
RUNNER = (ROOT / "bot" / "strategies" / "momentum_runner.py").read_text()
MOMENTUM = (ROOT / "bot" / "strategies" / "momentum.py").read_text()
SETTINGS = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
STRATS = yaml.safe_load((ROOT / "config" / "strategies.yaml").read_text())


def test_live_preflight_blocks_paper_workarounds():
    assert "LIVE PREFLIGHT FAILED" in MAIN
    assert "use_market_orders_on_bracket" in MAIN
    assert "ibkr_routing_exchange" in MAIN
    assert "LIVE_ALLOW_PAPER_WORKAROUNDS" in MAIN


def test_live_boot_balance_fail_closed():
    assert "LIVE MODE: IBKR balance sync returned $0" in ENGINE
    assert "keeping config starting_balance as fallback" in ENGINE


def test_portfolio_risk_ceiling_enforced():
    assert "Rule 8.6" in MANAGER
    assert "max_portfolio_risk" in MANAGER
    assert "entry * 0.03" in MANAGER


def test_portfolio_risk_math():
    def rule(positions, balance, ceiling_pct=0.20):
        open_risk = 0.0
        for p in positions:
            entry, qty, stop = p["entry_price"], p["quantity"], p["stop_loss"]
            if entry <= 0 or qty <= 0:
                continue
            per_share = (entry - stop) if 0 < stop < entry else entry * 0.03
            open_risk += max(0.0, per_share) * qty
        return open_risk < balance * ceiling_pct
    mk = lambda: {"entry_price": 10.0, "quantity": 1100, "stop_loss": 9.0}
    assert rule([mk()] * 3, 21_000) is True
    assert rule([mk()] * 4, 21_000) is False
    nostop = {"entry_price": 100.0, "quantity": 1400, "stop_loss": 0}
    assert rule([nostop], 21_000) is False


def test_crypto_sleeve_gate_exists_and_first():
    assert "_gate_crypto_sleeve_daily_loss" in ENGINE
    i_gate = ENGINE.find("reason = self._gate_crypto_sleeve_daily_loss(symbol)")
    i_spy = ENGINE.find("reason = self._gate_spy_circuit_breaker()")
    assert 0 < i_gate < i_spy


def test_crypto_sleeve_config():
    assert SETTINGS["crypto"]["risk"]["max_daily_loss_dollars"] == 300


def test_mutation_sites_locked():
    assert "resurrect a phantom entry" in ENGINE
    assert 'if symbol in self.positions:\n                    tight_stop' not in ENGINE
    assert ('if symbol in self.positions:\n                old_stop = '
            'self.positions[symbol].get("stop_loss", 0)\n                '
            'self.positions[symbol]["stop_loss"] = new_stop') not in ENGINE


def test_runner_score_normalized():
    assert RUNNER.count('"score": round(score / 12 * 100)') == 2
    assert '"score": score,' in RUNNER


def test_momentum_vol_floor_through_config():
    assert "vol_ratio >= max(1.5, self.vol_surge)" in MOMENTUM
    assert "and vol_ratio >= self.vol_surge" in MOMENTUM
    assert "vol_ratio >= 1.2" not in MOMENTUM


def test_untested_surfaces_disabled():
    assert STRATS["premarket_gap"]["enabled"] is False
    assert STRATS["tradingview_signals"]["enabled"] is False
    assert STRATS["mean_reversion"]["enabled"] is True
