"""IBKR sync must cancel pending SELL orders that have sat too long.

Live incident 2026-06-01: bot tried to flatten an orphaned DELL position
on Sunday night. The exit order (bracket STOP leg) went out as a LIMIT
SELL at a stale $313.88 reference. IBKR's price-cap rule blocked it
(market was $409+, cap allowed only $409.43 or more aggressive). The
order sat "Submitted" for 24+ hours. Each bot restart in that window
read the pending order via `openTrades()`, logged "Skipping DELL — has
pending SELL order", and bypassed normal sync. Position rode through
two trading days uncovered while the user kept seeing a phantom-orphan
P&L in the mirror that didn't match the bot's IBKR-side view.

Fix: check the AGE of pending SELL orders in the sync path. If older
than `stale_pending_sell_max_hours` (config; default 2.0h), cancel and
let normal sync resume. Fresh pending orders are unaffected — they
still skip-and-let-complete.

These tests pin three guarantees:
  1. Default threshold in shipped settings.yaml is 2.0 hours
  2. The engine sync code reads the threshold from risk_config (so
     ops can override without a code change)
  3. The engine code path contains the cancel + warning sequence with
     "STALE PENDING SELL CANCELLED" log marker
"""
from __future__ import annotations

import yaml


SETTINGS_PATH = "config/settings.yaml"


def test_shipped_default_is_2_hours():
    with open(SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    val = cfg.get("risk", {}).get("stale_pending_sell_max_hours")
    assert val == 2.0, (
        f"Shipped default must be 2.0 hours. Got {val!r}. If you bumped "
        f"the threshold, also update the docstring rationale."
    )


def test_engine_reads_threshold_from_risk_config():
    """Locked: the sync block must `risk_config.get(\"stale_pending_sell_max_hours\")`
    so ops can override without a code release. A direct hardcoded 2.0 in
    the engine would silently break the config override."""
    with open("bot/engine.py") as f:
        src = f.read()
    assert 'stale_pending_sell_max_hours' in src, (
        "engine.py must reference stale_pending_sell_max_hours by name "
        "(read from risk_config). A renamed/hardcoded threshold would "
        "regress this defence."
    )
    # Specifically asserts that it's read from the risk config dict
    # (vs a global constant or a hardcoded literal). The exact .get(...)
    # signature is locked.
    assert 'risk_config.get("stale_pending_sell_max_hours"' in src or \
           "risk_config.get('stale_pending_sell_max_hours'" in src, (
        "Threshold must be looked up via risk_config.get(...) so the "
        "config knob actually takes effect."
    )


def test_engine_has_cancel_path_for_stale_orders():
    """Locked: when age > threshold the code path must (a) call
    broker.cancel_order, (b) log the warning marker, and (c) `continue`
    instead of adding the symbol to the skip set. Removing any of those
    three quietly reintroduces the DELL pattern."""
    with open("bot/engine.py") as f:
        src = f.read()
    assert "STALE PENDING SELL CANCELLED" in src, (
        "Cancel path must log 'STALE PENDING SELL CANCELLED' so operators "
        "can grep for it after an incident."
    )
    assert "self.broker.cancel_order(" in src, (
        "Cancel path must call broker.cancel_order (we need IBKR to "
        "actually drop the stuck order, not just internal state)."
    )
    # Symbol must NOT end up in pending_sell_symbols after cancel — that
    # would still cause the sync to skip the position. The continue is the
    # marker that prevents that.
    cancel_section = src.split("STALE PENDING SELL CANCELLED")[1].split("except Exception")[0]
    assert "continue" in cancel_section, (
        "After cancelling a stale order the code must `continue` so the "
        "symbol is NOT added to pending_sell_symbols. Without this the "
        "sync still skips the position and the fix is a no-op."
    )


def test_threshold_is_configurable_per_deployment():
    """Sanity: defaults and overrides are both numeric, can be tuned per
    site. 2.0 today, an ops change to 4.0 or 0.5 should both work."""
    import importlib
    # We can't easily exec the engine without a full bot setup, but we
    # can verify the .get() signature includes a default fallback. Default
    # is needed so a missing config key doesn't crash sync.
    with open("bot/engine.py") as f:
        src = f.read()
    # Either form ("stale_pending_sell_max_hours", 2.0) or with kwarg.
    has_default = (
        '"stale_pending_sell_max_hours", 2.0' in src
        or "'stale_pending_sell_max_hours', 2.0" in src
    )
    assert has_default, (
        "risk_config.get() must include a default of 2.0 so a config "
        "without the key still works. Don't drop the fallback."
    )
