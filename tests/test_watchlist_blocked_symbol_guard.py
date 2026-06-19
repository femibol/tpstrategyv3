"""`add_to_watchlist` must reject symbols on `risk.blocked_symbols`.

2026-06-18 SOXL bleed (-$65) had two latent causes — PR #240 fixed the
YAML mis-nesting that made `blocked_symbols` invisible to the runtime
`_execute_signal` guard. This test pins a SECOND layer: even when the
runtime guard is wired correctly, a manually-added watchlist symbol
bypasses the scanner's leveraged-ETF class filter (the filter only runs
inside `IBKRBroker.scan_market`). One operator typo of
`add_to_watchlist("SOXL")` and the symbol enters every momentum
strategy's scanning universe — class filter never sees it.

Now `add_to_watchlist` checks `risk.blocked_symbols` first.

Companion fix: the `sp500_etfs` preset (a developer convenience that calls
`add_to_watchlist` for a curated list) previously held TQQQ / SOXL / ARKK.
TQQQ and SOXL are 3x leveraged ETFs (banned); ARKK is an active thematic
fund miscategorized as an S&P ETF. Pruned in the same change.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_engine_stub_with_blocked(blocked_list):
    """Build a minimal stub that exposes just enough surface for
    `TradingEngine.add_to_watchlist` to run without importing the
    full engine + IB + market-data stack."""
    from bot.engine import TradingEngine

    config = SimpleNamespace(
        risk_config={"blocked_symbols": list(blocked_list)},
        settings={},
    )

    # We don't need to construct a real TradingEngine — bind the method
    # to a stub that has just the attributes the method touches.
    stub = SimpleNamespace(
        config=config,
        watchlist=[],
        market_data=None,
        strategies={},
    )
    # Bind the methods we need
    stub.add_to_watchlist = TradingEngine.add_to_watchlist.__get__(stub)
    stub._inject_symbol_into_strategies = (
        TradingEngine._inject_symbol_into_strategies.__get__(stub)
    )
    return stub


def test_add_to_watchlist_rejects_blocked_symbol():
    """The defining case: SOXL is blocked at YAML; manual add must fail."""
    engine = _make_engine_stub_with_blocked(["SOXL", "TQQQ", "JNUG"])
    result = engine.add_to_watchlist("SOXL")
    assert "SOXL" not in result, (
        "SOXL was added to watchlist despite being on risk.blocked_symbols — "
        "the next person who runs `add_to_watchlist('SOXL')` will recreate "
        "the 2026-06-18 bleed."
    )


def test_add_to_watchlist_rejects_blocked_lowercase_input():
    """Operator typing `soxl` should also be rejected — case-insensitive."""
    engine = _make_engine_stub_with_blocked(["SOXL"])
    result = engine.add_to_watchlist("soxl")
    assert "SOXL" not in result and "soxl" not in result


def test_add_to_watchlist_accepts_clean_symbol():
    """Don't over-correct — clean symbols still flow through."""
    engine = _make_engine_stub_with_blocked(["SOXL", "TQQQ"])
    result = engine.add_to_watchlist("AAPL")
    assert "AAPL" in result


def test_add_to_watchlist_handles_empty_blocklist():
    """If `risk.blocked_symbols` isn't set (test-mode configs), don't crash."""
    config = SimpleNamespace(risk_config={}, settings={})
    from bot.engine import TradingEngine
    stub = SimpleNamespace(
        config=config, watchlist=[], market_data=None, strategies={}
    )
    stub.add_to_watchlist = TradingEngine.add_to_watchlist.__get__(stub)
    stub._inject_symbol_into_strategies = (
        TradingEngine._inject_symbol_into_strategies.__get__(stub)
    )
    result = stub.add_to_watchlist("AAPL")
    assert "AAPL" in result


def test_sp500_etfs_preset_has_no_leveraged_tickers():
    """The preset list itself shouldn't contain leveraged ETFs. Even
    though `add_to_watchlist` now rejects them, the preset should
    represent intent — and the intent is broad-market index ETFs."""
    from bot.engine import TradingEngine
    preset = TradingEngine.WATCHLIST_PRESETS.get("sp500_etfs", {})
    symbols = preset.get("symbols", [])
    banned = {"SOXL", "SOXS", "TQQQ", "SQQQ", "SPXU", "SPXS", "TZA", "TNA",
              "FAS", "FAZ", "UVXY", "UVIX", "HIBL", "HIBS"}
    leveraged_in_preset = [s for s in symbols if s in banned]
    assert not leveraged_in_preset, (
        f"sp500_etfs preset contains leveraged ETFs: {leveraged_in_preset}. "
        f"They'll be rejected at add_to_watchlist now, but having them in "
        f"the preset is misleading — broad-market ETFs only."
    )


def test_blocked_log_message_present():
    """Anti-regression at source level — make sure the guard's log line
    survives future refactors. The log line is how operators discover
    the rejection."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    assert "WATCHLIST BLOCKED" in src, (
        "bot/engine.py: `WATCHLIST BLOCKED` log line removed — operators "
        "won't see why a watchlist add silently failed. Restore it."
    )
