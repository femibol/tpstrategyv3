"""Dashboard tab consolidation (Wave-6 UI overhaul follow-up).

Live mobile review surfaced that the bottom tab strip was unusable:
13 tabs cramped into a horizontal scroll bar, names truncated
("Positi", "Signa", "Histor"). Operator-facing UX bug — a
"professional system" wants a small, intentional set of primary
destinations and tucks rare/admin views behind a menu.

Fix: 5 primary tabs + a "⋯ More" popover containing the rest. The
backing tab-content divs and switchTab() target IDs are unchanged
so every existing data path (lazy-load history, tab-conditional
rendering, etc.) keeps working.

Primary 5 (visible):
  Positions, Trades, Stats (performance), Scanner, ⋯ More

Inside ⋯ More popover:
  Strategies, History, Daily Stats, Signals, RVOL, Watchlist,
  Analysis, Alerts, Settings

These tests pin:
  1. Exactly 5 primary tabs visible (one of which is "more-btn")
  2. The 4 non-More primary tabs map to the operator's main intents
  3. The More popover holds the remaining 9 tabs as menu items
  4. Every data-tab value from the old 13-tab strip is preserved
     somewhere (so switchTab() never null-references)
  5. switchTab handles a tab that lives in the menu (no crash)
  6. JS handlers toggleMoreMenu / closeMoreMenu / switchTabFromMenu
     are defined
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = Path(__file__).parent.parent / "bot" / "dashboard" / "templates" / "dashboard.html"


def _tabs_markup_block(html):
    """Locate the HTML markup region with the new primary tab strip."""
    anchor_marker = '<!-- TABS — consolidated to 5 primary'
    idx = html.find(anchor_marker)
    assert idx > 0, "primary tabs markup block not found"
    return html[idx:idx + 4000]


# === 1. Primary tab bar ===


def test_primary_tab_bar_has_five_entries():
    """Exactly 5 elements in the primary <div class='tabs'> — 4 normal
    tabs + 1 More button. More than that is the old crammed strip."""
    html = HTML.read_text()
    block = _tabs_markup_block(html)
    # Find the <div class="tabs">…</div> closing immediately after the comment
    tabs_start = block.find('<div class="tabs">')
    tabs_end = block.find('</div>\n\n<!-- More-menu popover')
    assert tabs_start >= 0 and tabs_end > tabs_start
    tabs_inner = block[tabs_start:tabs_end]
    primary_tab_count = tabs_inner.count('class="tab ') + tabs_inner.count('class="tab"')
    # "tab active" (positions), "tab" × 3, "tab more-btn" = 5
    assert primary_tab_count == 5, (
        f"Expected 5 primary tabs (4 core + More), found {primary_tab_count}. "
        "Don't add bloat — consolidate behind More instead."
    )


def test_primary_tabs_are_the_high_frequency_four():
    """The 4 non-More primary tabs are the operator's most-used
    destinations: open positions, recent trades, performance stats,
    market scanner. Pinning to lock the info architecture."""
    html = HTML.read_text()
    block = _tabs_markup_block(html)
    primary_anchor = block[:block.find('<!-- More-menu popover')]
    must_be_primary = ["positions", "trades", "performance", "scanner"]
    for tab in must_be_primary:
        assert f'data-tab="{tab}"' in primary_anchor, (
            f"primary tab '{tab}' missing from the top bar"
        )


def test_more_button_present():
    html = HTML.read_text()
    block = _tabs_markup_block(html)
    primary_anchor = block[:block.find('<!-- More-menu popover')]
    assert "more-btn" in primary_anchor, "More button missing"
    assert "toggleMoreMenu" in primary_anchor


# === 2. More menu popover ===


def test_more_menu_contains_nine_items():
    html = HTML.read_text()
    block = _tabs_markup_block(html)
    # Count menu items
    menu_idx = block.find('<div class="more-menu"')
    assert menu_idx > 0
    menu_end = block.find('</div>', menu_idx + 1000)
    menu_inner = block[menu_idx:menu_end + 6]
    count = menu_inner.count("more-menu-item")
    # Class definition on the items only (not the .more-menu-item css selector)
    # — both will count, but the markup uses class= once per item so the count
    # matches item count exactly
    assert count == 9, (
        f"Expected 9 items in More popover, found {count}. Adjust the IA "
        "split or move items in/out of the menu."
    )


def test_more_menu_holds_archived_tabs():
    """The 9 hidden tabs in the More menu are: Strategies, History,
    Daily Stats, Signals (suggestions), RVOL, Watchlist, Analysis,
    Alerts (notifications), Settings."""
    html = HTML.read_text()
    block = _tabs_markup_block(html)
    expected = ["strategies", "history", "daily", "suggestions", "rvol",
                "watchlist", "analysis", "notifications", "settings"]
    for tab in expected:
        # data-tab="X" appears on the more-menu-item line
        assert f'data-tab="{tab}"' in block, f"hidden tab '{tab}' missing from More menu"


# === 3. Anti-regression: all 13 historical data-tab values preserved ===


def test_all_thirteen_tab_targets_still_addressable():
    """Each tab-content div maps to a data-tab value; switchTab(name)
    looks up `#tab-name`. If we removed a data-tab value during the
    consolidation, the corresponding `<div id="tab-X">` content
    block becomes unreachable. Pin that every original ID still has
    a clicker somewhere."""
    html = HTML.read_text()
    original_tab_ids = [
        "positions", "suggestions", "trades", "rvol", "scanner",
        "history", "watchlist", "performance", "strategies",
        "analysis", "daily", "settings", "notifications",
    ]
    for tab in original_tab_ids:
        assert f'data-tab="{tab}"' in html, (
            f"tab '{tab}' has no clicker anywhere (primary bar or More menu) "
            "— its content block is unreachable"
        )


# === 4. switchTab safe for menu-only tabs ===


def test_switch_tab_handles_menu_only_tabs():
    """Old switchTab() called .querySelector('.tab[data-tab=X]').classList.add(active)
    — if X is in the More menu, no .tab element matches and the old
    code threw a null-reference. New code must handle that path."""
    html = HTML.read_text()
    fn_idx = html.find("function switchTab(")
    assert fn_idx > 0
    body = html[fn_idx:fn_idx + 2000]
    # Anti-regression: the unconditional `.querySelector(...).classList.add(...)`
    # pattern must be gone — replaced with the null-safe version
    assert "var primaryTab = document.querySelector" in body, (
        "switchTab() must defensively look up the primary tab and handle "
        "the menu-only case"
    )
    assert "more-menu-item[data-tab=" in body, (
        "switchTab() must fall back to highlighting the More menu item"
    )


# === 5. Menu JS handlers present ===


def test_menu_toggle_handlers_defined():
    html = HTML.read_text()
    for fn in ["function toggleMoreMenu", "function closeMoreMenu",
               "function switchTabFromMenu"]:
        assert fn in html, f"{fn} not defined"


def test_menu_closes_on_backdrop_click():
    html = HTML.read_text()
    bd_idx = html.find('id="moreMenuBackdrop"')
    assert bd_idx > 0
    # Backdrop's onclick triggers close
    bd_line = html[bd_idx:bd_idx + 200]
    assert "closeMoreMenu" in bd_line


# === 6. CSS for the popover present ===


def test_more_menu_css_styles_present():
    html = HTML.read_text()
    assert ".more-menu {" in html, "More menu container CSS missing"
    assert ".more-menu.open" in html, "More menu open-state CSS missing"
    assert ".more-menu-item" in html
    assert ".more-menu-backdrop" in html
