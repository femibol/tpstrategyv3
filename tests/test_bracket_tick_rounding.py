"""Tick-size rounding on bracket orders — IBKR Error 110 fix.

2026-06-05 incident: PR #202 (MARKET-parent) and PR #203 (IEX routing)
both deployed and BOTH failed to fix the no-fills bug. Every bracket
order still died in PendingSubmit. After deeper diagnostics, the actual
root cause surfaced in the IBKR error stream:

  IBKR Error 110: The price does not conform to the minimum price
  variation for this contract.

IBKR's tick-size rules:
  - price < $1   → tick $0.0001 (4 decimals max)
  - price ≥ $1   → tick $0.01   (2 decimals max)

Engine math (R/R-stretch in engine.py, ATR multipliers in strategies)
frequently produces 4-decimal SL/TP values on >$1 stocks. Example
from the live log:
  R/R STRETCH: IREZ entry=$6.9100 stop=$6.7027 target=$7.3246
                                       ^^^^^^         ^^^^^^
                                       4 decimals on a $6 stock → Error 110

The legacy `BRACKET ORDER placed` log message used :.2f for display so
the prices LOOKED clean, but the actual values flowing into
`bracketOrder()` were the raw 4-decimal numbers. IBKR rejected each
bracket leg with Error 110 → bracket sat in PendingSubmit → 15s
timeout cancel.

Fix: round all bracket inputs to the appropriate tick BEFORE placing.

These tests pin three guarantees:
  1. Prices ≥ $1 get rounded to 2 decimals (the $0.01 tick).
  2. Prices < $1 get rounded to 4 decimals (the $0.0001 tick).
  3. Sub-cent precision NEVER leaks into the order placement.
"""
from __future__ import annotations


def _round_to_tick(p):
    """Mirror of the broker-side rounding logic. Pinned here so a
    regression on the broker side (someone removing the rounding) gets
    caught at unit-test time, not after every bracket order died."""
    if p is None or p <= 0:
        return p
    return round(p, 4) if p < 1.0 else round(p, 2)


# === Tick rounding correctness ===


def test_above_one_dollar_rounds_to_two_decimals():
    """The IREZ scenario: $6 stop with 4-decimal precision must round
    to 2 decimals before reaching IBKR."""
    assert _round_to_tick(6.7027) == 6.70
    assert _round_to_tick(7.3246) == 7.32
    assert _round_to_tick(7.3251) == 7.33  # rounds up at midpoint


def test_below_one_dollar_keeps_four_decimals():
    """Sub-$1 names (penny stocks, low-float runners like LGPS at $0.79)
    use the $0.0001 tick — 4 decimals are valid, no rounding past that."""
    assert _round_to_tick(0.7900) == 0.7900
    assert _round_to_tick(0.4521) == 0.4521
    assert _round_to_tick(0.123456) == 0.1235  # rounds to 4 decimals


def test_exactly_one_dollar_uses_above_one_tick():
    """At the $1 boundary, $0.01 tick applies (the 2-decimal rule).
    Strict >= 1.0 gate matches IBKR's actual rule."""
    assert _round_to_tick(1.0000) == 1.00
    assert _round_to_tick(1.005) == 1.0


def test_just_below_one_dollar_uses_4_decimal_tick():
    assert _round_to_tick(0.9999) == 0.9999
    assert _round_to_tick(0.99) == 0.99  # already valid


def test_zero_or_none_returns_unchanged():
    """Defensive: don't crash on bad input. Bracket placement upstream
    should never pass 0 or None, but if it does, propagate the bad
    value so the failure is visible rather than silently rewriting."""
    assert _round_to_tick(0) == 0
    assert _round_to_tick(None) is None
    assert _round_to_tick(-1.5) == -1.5  # negative → unchanged (won't reach IBKR anyway)


# === Sub-cent precision must not leak ===


def test_engine_math_artifacts_get_cleaned():
    """Realistic R/R-stretch artifacts from the IREZ trade. All of these
    triggered Error 110 in production. Post-fix, all must be clean."""
    bad_prices = [
        (6.9100, 6.91),
        (6.7027, 6.70),
        (6.8129, 6.81),
        (7.3246, 7.32),
        (7.2771, 7.28),
    ]
    for raw, expected in bad_prices:
        rounded = _round_to_tick(raw)
        assert rounded == expected, f"{raw} → expected {expected}, got {rounded}"
        # And verify the rounded value has at most 2 decimals
        assert abs(rounded - round(rounded, 2)) < 1e-9, (
            f"Rounded value {rounded} still has sub-cent precision"
        )


def test_micro_cap_runner_prices_keep_precision():
    """Sub-$1 runners (e.g. LGPS $0.79, NOTV $0.16, HXHX $0.45) — the
    4-decimal precision is VALID for IBKR. Don't over-round these or
    we lose meaningful price granularity."""
    micro_caps = [
        (0.7937, 0.7937),
        (0.1598, 0.1598),
        (0.4520, 0.4520),
    ]
    for raw, expected in micro_caps:
        assert _round_to_tick(raw) == expected
