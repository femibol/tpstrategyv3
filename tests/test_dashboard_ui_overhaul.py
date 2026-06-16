"""Dashboard UI overhaul (Wave-6) — info hierarchy + live feed redesign.

Live mobile review (2026-06-16) drove three improvements after the
bugfix PR fixed the lying-cards:

  1. Information hierarchy. The 3x2 stat grid treated all six cards
     equally — Today P&L sat next to Market Regime in the same visual
     weight, even though the operator looks at P&L 95% of the time.
     Now: hero card (Today P&L, larger), secondary row (Balance,
     Positions), tertiary row (Win Rate, Drawdown, Regime).

  2. Theme polish. The gradient top-edge accent appeared on every card,
     so the eye couldn't pick out which one mattered. Restricted to the
     hero card. Body radial-gradients dialed down (was 0.10 / 0.08
     alpha; now 0.06 / 0.04).

  3. Live Feed redesign. Previously read only from notifier.history,
     which is in-memory and empty across restarts — the operator saw
     "Waiting for activity..." despite 200+ trades on disk. Now
     backfills from the trades list (color-coded by P&L), blended
     with any live notifier entries by time.

These tests pin the structural changes — the rendered visuals
themselves can't be unit-tested without a browser harness, so the
asserts target template strings and CSS class definitions.
"""
from __future__ import annotations

from pathlib import Path

HTML = Path(__file__).parent.parent / "bot" / "dashboard" / "templates" / "dashboard.html"


# === 1. Hierarchy: three bands instead of one flat grid ===


def _stats_markup_block(html):
    """Locate the HTML markup region (not CSS) that holds the new
    hero/secondary/tertiary structure."""
    anchor = html.find('<div class="stats-hero">')
    assert anchor > 0, "stats-hero HTML block not found"
    return html[anchor:anchor + 3000]


def test_hero_band_present():
    html = HTML.read_text()
    block = _stats_markup_block(html)
    assert '<div class="stat-card hero">' in block, "hero card class not applied"
    assert "Today P&L" in block, "Today P&L not in hero band"
    assert 'id="dailyPnl"' in block


def test_secondary_band_present():
    html = HTML.read_text()
    block = _stats_markup_block(html)
    assert '<div class="stats-secondary">' in block
    assert "Balance" in block
    assert "Positions" in block


def test_tertiary_band_demotes_regime_and_drawdown():
    html = HTML.read_text()
    block = _stats_markup_block(html)
    assert '<div class="stats-tertiary">' in block
    assert '<div class="stat-card tert">' in block
    assert "Win Rate" in block
    assert "Drawdown" in block
    assert "Regime" in block


def test_old_flat_grid_removed():
    """Anti-regression: the old `<div class="stats">` flat grid must be
    gone — its 6 equal-weight cards are what the overhaul replaces."""
    html = HTML.read_text()
    # Search for the literal opening tag pattern
    assert 'class="stats"' not in html, (
        "old flat 6-card grid (.stats) still in template — overhaul "
        "did not replace it"
    )


# === 2. Theme polish — gradient edge confined to hero ===


def test_gradient_edge_only_on_hero():
    """The gradient ::before edge is restricted to the hero card so
    only the operator's primary number gets the visual accent."""
    html = HTML.read_text()
    # Hero gets its own ::before
    assert ".stat-card.hero::before" in html
    # The unscoped `.stat-card::before` rule from the old CSS must be gone
    # (every card used to get the gradient edge, drowning out the hero)
    assert ".stat-card::before" not in html, (
        "unscoped .stat-card::before still present — every card gets "
        "the gradient accent again"
    )


def test_body_radial_gradients_dialed_down():
    """Background radial-gradients are decorative; the old 0.10/0.08
    alpha created a fog effect that fought with the foreground cards."""
    html = HTML.read_text()
    # Old high-alpha values should be gone in the body background block
    body_idx = html.find("body {")
    block = html[body_idx:body_idx + 1000]
    # Both values reduced
    assert "rgba(91,140,255,0.06)" in block
    assert "rgba(155,125,255,0.04)" in block
    # Old higher values shouldn't appear in this block
    assert "rgba(91,140,255,0.10)" not in block
    assert "rgba(155,125,255,0.08)" not in block


def test_tertiary_card_styling_present():
    """Tertiary cards must be visually demoted (smaller value, denser
    padding) so the operator's eye lands on the hero first."""
    html = HTML.read_text()
    assert ".stat-card.tert" in html
    # The override making the tert value smaller than the default
    assert ".stat-card.tert .stat-value" in html


# === 3. Live Feed — backfilled from trades ===


def test_live_feed_reads_from_trades():
    """The feed must compose from `trades` (always available from disk)
    not just `notifs` (in-memory, empty after restart)."""
    html = HTML.read_text()
    # Find the live feed render block
    idx = html.find("// Live Feed")
    assert idx > 0, "Live Feed marker not found"
    block = html[idx:idx + 4000]
    assert "trades && trades.length > 0" in block, (
        "Live Feed not consuming trades for backfill"
    )
    # P&L color-coded in the trade rows
    assert "var sign = r.pnl >= 0 ?" in block, (
        "Live Feed trade rows missing P&L sign/color logic"
    )


def test_live_feed_blends_trades_and_notifs():
    """Both kinds of rows are merged + time-sorted, newest first.
    Without merging, transient notifs would be hidden behind backfilled
    trades or vice versa."""
    html = HTML.read_text()
    idx = html.find("// Live Feed")
    block = html[idx:idx + 4000]
    assert "kind: 'trade'" in block
    assert "kind: 'notif'" in block
    assert "rows.sort" in block, "Live Feed entries not time-sorted"


def test_live_feed_caps_visible_rows():
    """Cap at 15 rows so a long history doesn't pin the feed open."""
    html = HTML.read_text()
    idx = html.find("// Live Feed")
    block = html[idx:idx + 4000]
    assert "rows = rows.slice(0, 15)" in block


def test_live_feed_falls_back_to_empty_state_message():
    """Day-0 fresh install: no trades, no notifs → show 'Waiting for
    activity...' instead of an empty list (which renders as nothing)."""
    html = HTML.read_text()
    idx = html.find("// Live Feed")
    block = html[idx:idx + 4000]
    assert "rows.length === 0" in block
    assert "Waiting for activity" in block


# === 4. Sanity: ids still referenced by other JS ===


def test_critical_card_ids_preserved():
    """The card layout changed but every `id=` consumed elsewhere in
    the page must still exist — otherwise other JS modules silently
    fail to find their target element."""
    html = HTML.read_text()
    must_exist = [
        'id="balance"', 'id="totalReturn"',
        'id="dailyPnl"', 'id="dailyTrades"',
        'id="openPositions"', 'id="strategies"',
        'id="drawdown"', 'id="peakBalance"',
        'id="regimeBadge"', 'id="regimeRisk"',
        'id="winRate"', 'id="profitFactor"',
        'id="liveFeed"',
    ]
    for _id in must_exist:
        assert _id in html, f"{_id} element missing from template"
