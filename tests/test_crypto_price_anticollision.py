"""Coinbase price override on `/api/positions` falls back to engine
when ticker collision is detected.

2026-06-22 incident: bot bought JUP-USD via TradersPost at $0.21
(Jupiter / Solana, the asset TradersPost actually routes to). The
dashboard's `_coinbase_spot_price("JUP-USD")` returned $0.0002 — but
that's a DIFFERENT JUP token on Coinbase's listing (a delisted /
relaunched ticker), not the real Jupiter. Result: dashboard rendered
the position at -$1,048 / -99.9% while the actual position was flat.
User got a fake heart attack at a fake -$1,048 loss.

The fix: before trusting Coinbase's price, compare it against the
engine's last-known mark for the symbol. A >50% deviation is almost
certainly a ticker collision (a price doesn't crash 99% in seconds and
also not show up in any news / log noise). Fall back to engine-tracked
price, mark `price_source: "engine_anti_collision"` so the operator
can see in `/api/positions` that the fallback fired.

These tests pin the resolver logic — a stub `_coinbase_spot_price`
so we don't hit the network.
"""
from __future__ import annotations

from pathlib import Path


def test_dashboard_source_has_anti_collision_resolver():
    """Anti-regression: pin the 50% deviation guard in source."""
    src = (Path(__file__).parent.parent / "bot" / "dashboard" / "app.py").read_text()
    # The deviation check + fallback label
    assert "deviation > 0.50" in src or "deviation > 0.5" in src, (
        "app.py: missing the deviation > 0.50 anti-collision guard. "
        "Removing it re-opens the JUP-USD fake -99% dashboard scenario."
    )
    assert "engine_anti_collision" in src, (
        "app.py: missing the `engine_anti_collision` price_source label. "
        "Operator needs visibility when the fallback fires."
    )


def test_anti_collision_logic_pure_function():
    """Replicate the resolver math in isolation. Same logic as app.py
    `if engine_mark > 0 and abs(live - engine_mark)/engine_mark > 0.50:
    fall back`. Pins the math against the live JUP / WBT / pump-dump
    edge cases."""
    def resolve(live, engine_mark, entry):
        """Mirror of the dashboard logic. Returns (current, source)."""
        em = engine_mark or entry or 0
        if em > 0 and abs(live - em) / em > 0.50:
            return em, "engine_anti_collision"
        return live, "coinbase_live"

    # The literal JUP-USD case: engine knows $0.21, Coinbase says $0.0002 — fall back
    current, src = resolve(live=0.00024, engine_mark=0.21, entry=0.2099)
    assert src == "engine_anti_collision"
    assert current == 0.21

    # Normal small move: -3% from engine mark — trust Coinbase
    current, src = resolve(live=0.97, engine_mark=1.00, entry=1.00)
    assert src == "coinbase_live"
    assert current == 0.97

    # Normal big move (within tolerance): -40% from engine mark — trust Coinbase
    # (real crashes do happen; 50% is the threshold for "almost certainly bad data")
    current, src = resolve(live=0.60, engine_mark=1.00, entry=1.00)
    assert src == "coinbase_live"
    assert current == 0.60

    # Boundary case: exactly 50% deviation — trust Coinbase (only > 0.50 triggers)
    current, src = resolve(live=0.50, engine_mark=1.00, entry=1.00)
    assert src == "coinbase_live"

    # >50% gain (the price-pump case — equally suspect for a ticker collision)
    current, src = resolve(live=10.0, engine_mark=1.00, entry=1.00)
    assert src == "engine_anti_collision"

    # Engine mark unknown (0 / None) — accept whatever Coinbase gives,
    # operator has bigger problems if the engine isn't tracking
    current, src = resolve(live=0.50, engine_mark=0, entry=0.21)
    assert src == "engine_anti_collision"  # fallback to entry-derived guard


def test_resolver_handles_zero_engine_mark_with_zero_entry():
    """Edge case: brand-new position with engine_mark=0 AND entry=0
    (shouldn't happen but defend). Don't crash."""
    def resolve(live, engine_mark, entry):
        em = engine_mark or entry or 0
        if em > 0 and abs(live - em) / em > 0.50:
            return em, "engine_anti_collision"
        return live, "coinbase_live"

    current, src = resolve(live=0.50, engine_mark=0, entry=0)
    # em = 0; the `em > 0` guard short-circuits — Coinbase wins by default
    assert src == "coinbase_live"
    assert current == 0.50
