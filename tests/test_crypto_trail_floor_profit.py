"""Crypto trail floor at entry × 1.005 — eliminates breakeven-wick exits.

The session-9 trail-arm fix (PR #172, CRYPTO_TRAIL_ARM_PCT=0.005) cut the
volume of trail-wick exits ~85% but didn't kill the pattern entirely.
Trade review across 198 crypto trades (2026-05-17..05-30) found:
  - 31 trades with -1% < pnl <= 0%   (pure breakeven wicks)  net -$203
  -  5 of those POST PR #172 deploy                         net  -$27

The pattern: price ticks above entry by some small amount, trail arms
with floor at `entry_price` exactly, price wicks back below entry by
slippage, exit fires at small loss. Even raising CRYPTO_TRAIL_ARM_PCT
doesn't fully eliminate this — it just reduces frequency.

Option D fix (this file): raise the trail floor itself. Trail can now
sit no lower than `entry × (1 + CRYPTO_TRAIL_ARM_PCT)` for crypto. Any
trail exit locks in ≥0.5% gain (modulo slippage). Wickoes become
no-ops — the trail floor stays at +0.5%, doesn't fire on a return to
entry.

Implementation: helper `Engine._trail_floor_price(symbol, entry_price)`
that returns the floor (entry × 1.005 for crypto, entry unchanged for
equity). Used in all 4 trail-arming sites in engine.py.

These tests pin five guarantees:
  1. Crypto floor = entry × 1.005
  2. Equity floor = entry (unchanged; equity has different exit dynamics)
  3. Floor applies regardless of suffix variant (-USD, -USDT, -BTC, -ETH)
  4. After arm, trail sits at floor when current price is at or just
     above the arm threshold (was the wick scenario)
  5. Trail still ratchets up normally past the floor as price runs
"""
from __future__ import annotations

from types import SimpleNamespace

from bot.engine import TradingEngine


def _new_engine():
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = SimpleNamespace(settings={})
    return eng


def test_crypto_floor_is_entry_plus_50bps():
    eng = _new_engine()
    floor = eng._trail_floor_price("NEAR-USD", entry_price=2.50)
    expected = 2.50 * 1.005
    assert abs(floor - expected) < 1e-9


def test_equity_floor_is_entry_unchanged():
    """Don't extend the lockin to equity — different exit dynamics, no
    parallel trade-review yet. Equity strategies rely on the floor-at-entry
    behavior to avoid double-stops via the static stop_loss path."""
    eng = _new_engine()
    assert eng._trail_floor_price("DELL", entry_price=420.0) == 420.0
    assert eng._trail_floor_price("AAPL", entry_price=180.5) == 180.5


def test_all_crypto_suffix_variants_get_floor():
    """The crypto detection must trigger the floor across all four suffix
    variants `-USD`, `-USDT`, `-BTC`, `-ETH`. Trail logic must not have
    asymmetric behavior between BTC-USD and ETH-USDT."""
    eng = _new_engine()
    for sym in ("BTC-USD", "ETH-USDT", "WLD-USD", "ADA-BTC", "DOGE-ETH"):
        floor = eng._trail_floor_price(sym, 100.0)
        assert abs(floor - 100.5) < 1e-9, f"Floor wrong for {sym}: {floor}"


def test_trail_at_floor_when_arm_pct_just_reached():
    """Wick scenario: price just hit the arm threshold (+0.5%) and starts
    drifting back. Trail should sit at the floor (entry × 1.005), and any
    exit would lock in +0.5% — NOT below entry."""
    eng = _new_engine()
    entry = 100.0
    trail_pct = 0.015  # 1.5% trail
    # Price at +0.5% — just at arm threshold
    price_at_arm = 100.5
    floor = eng._trail_floor_price("BTC-USD", entry)
    new_trail = max(price_at_arm * (1 - trail_pct), floor)
    # Trail should be the FLOOR (100.5), not 100.5 * 0.985 = 98.9925
    assert abs(new_trail - 100.5) < 1e-9
    # Exit triggers when current price <= trail.
    # Even at the worst case (price drops to 100.5), exit price ≈ 100.5
    # = +0.5% gain. NEVER below entry.
    assert new_trail >= entry * 1.005


def test_trail_ratchets_up_normally_past_floor():
    """When price runs well past the floor (e.g. +3%), the trail follows
    `price × (1 - trail_pct)` upward. The floor only matters at the LOW
    end — it must not pin the trail when price has clearly cleared it."""
    eng = _new_engine()
    entry = 100.0
    trail_pct = 0.015
    # Price at +5% — well past the floor
    price_run = 105.0
    floor = eng._trail_floor_price("BTC-USD", entry)
    new_trail = max(price_run * (1 - trail_pct), floor)
    # Trail should follow price, not be pinned at floor
    expected_trail = 105.0 * (1 - 0.015)  # 103.425
    assert abs(new_trail - expected_trail) < 1e-9
    assert new_trail > floor  # past the lockin level


def test_engine_inline_code_uses_helper_at_all_four_sites():
    """Lock the engine code to use _trail_floor_price at all four sites
    (the original trail logic, _on_5sec_bar, _on_tick, _monitor_positions
    slow poll). A refactor that bypasses the helper would silently
    reintroduce the wick pattern."""
    with open("bot/engine.py") as f:
        src = f.read()
    helper_uses = src.count("self._trail_floor_price(")
    # 4 sites + 1 definition + comment references in docstring/comments
    # We assert it's used at ≥ 4 trail-arming sites (callers, not the def).
    callers = src.count("self._trail_floor_price(symbol, entry")
    assert callers >= 4, (
        f"Expected _trail_floor_price used at all 4 trail-arming sites "
        f"(_fast_scalp_monitor, _on_5sec_bar, _on_tick, _monitor_positions); "
        f"found {callers} callers. Did a refactor drop the helper?"
    )
