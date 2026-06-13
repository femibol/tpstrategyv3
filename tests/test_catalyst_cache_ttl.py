"""Catalyst cache TTL (Wave 5).

momentum_runner._catalyst_cache had no TTL. PR #210 wired
NewsFeed.get_catalyst_map (which filters at SEND time by a 4h
lookback) → momentum_runner.feed_catalyst_data via dict.update().
Two failure modes that the SEND-side filter doesn't catch:

  1. Once a catalyst is in the cache, it STAYS forever even if the
     underlying news ages past 4h. The engine wires
     `feed_catalyst_data` from inside a `if catalyst_map:` block
     — if the new map is empty, the cache isn't touched.
  2. Across days, the cache grows unbounded (every symbol ever
     fed catalyst data stays in the dict).

Fix: stamp each entry with `_fed_at` on write, drop entries older
than `catalyst_ttl_secs` (default 4h) on read AND pop them from
the dict so it doesn't grow.

These tests pin:
  1. feed_catalyst_data stamps _fed_at
  2. _catalyst_for returns fresh entries
  3. _catalyst_for drops + returns None for stale entries
  4. Stale entries are removed from the cache (no leak)
  5. TTL is configurable via catalyst_ttl_secs
  6. Default 4h matches news feed lookback (240min)
  7. Legacy entries without _fed_at pass through (deploy-time safety)
"""
from __future__ import annotations

import time
from types import SimpleNamespace


def _make_runner(cfg_overrides=None):
    from bot.strategies.momentum_runner import MomentumRunnerStrategy
    from bot.data.indicators import TechnicalIndicators
    cfg = {
        "min_score": 6, "min_price": 1, "max_price": 100,
        "min_volume": 100000, "max_daily_change_pct": 30,
        "max_open_positions": 5, "atr_stop_multiplier": 1.0,
    }
    if cfg_overrides:
        cfg.update(cfg_overrides)
    return MomentumRunnerStrategy(cfg, TechnicalIndicators(), capital=10000)


# === 1. Stamp + fresh read ===


def test_feed_stamps_fed_at():
    runner = _make_runner()
    runner.feed_catalyst_data({
        "AAPL": {"type": "fda", "score": 3},
    })
    cached = runner._catalyst_cache.get("AAPL")
    assert cached is not None
    assert "_fed_at" in cached
    # Stamped to roughly now
    assert abs(cached["_fed_at"] - time.time()) < 1.0


def test_fresh_entry_returned():
    """Just-fed catalyst within TTL must be readable."""
    runner = _make_runner()
    runner.feed_catalyst_data({"AAPL": {"type": "fda", "score": 3}})
    entry = runner._catalyst_for("AAPL")
    assert entry is not None
    assert entry["type"] == "fda"


def test_no_overwrite_of_existing_fed_at():
    """If the feed passes an explicit `_fed_at` (e.g. from a producer
    that knows the news's original publish time), keep that value
    instead of stamping over it."""
    runner = _make_runner()
    older = time.time() - 100
    runner.feed_catalyst_data({
        "AAPL": {"type": "fda", "score": 3, "_fed_at": older},
    })
    cached = runner._catalyst_cache.get("AAPL")
    assert cached["_fed_at"] == older


# === 2. TTL expiry ===


def test_stale_entry_returns_none():
    """An entry stamped older than TTL must read as None."""
    runner = _make_runner({"catalyst_ttl_secs": 60})  # 1-min TTL
    runner._catalyst_cache["AAPL"] = {
        "type": "fda", "score": 3,
        "_fed_at": time.time() - 120,  # 2 min old > 1-min TTL
    }
    assert runner._catalyst_for("AAPL") is None


def test_stale_entry_removed_from_cache():
    """Anti-bloat: stale read also pops the entry so the dict shrinks."""
    runner = _make_runner({"catalyst_ttl_secs": 60})
    runner._catalyst_cache["AAPL"] = {
        "type": "fda", "_fed_at": time.time() - 120,
    }
    runner._catalyst_for("AAPL")
    assert "AAPL" not in runner._catalyst_cache


def test_boundary_just_inside_ttl_still_fresh():
    runner = _make_runner({"catalyst_ttl_secs": 100})
    runner._catalyst_cache["AAPL"] = {
        "type": "fda", "_fed_at": time.time() - 50,  # 50s < 100s
    }
    assert runner._catalyst_for("AAPL") is not None


def test_boundary_just_outside_ttl_stale():
    runner = _make_runner({"catalyst_ttl_secs": 50})
    runner._catalyst_cache["AAPL"] = {
        "type": "fda", "_fed_at": time.time() - 100,
    }
    assert runner._catalyst_for("AAPL") is None


# === 3. Configurability + defaults ===


def test_default_ttl_matches_news_feed_lookback():
    """Default TTL 4h = 14400s = 240min matches
    NewsFeed.get_catalyst_map(lookback_minutes=240)."""
    runner = _make_runner()  # no override
    assert runner._catalyst_ttl_secs == 4 * 60 * 60


def test_configurable_ttl():
    runner = _make_runner({"catalyst_ttl_secs": 3600})  # 1h
    assert runner._catalyst_ttl_secs == 3600


# === 4. Legacy entry safety ===


def test_legacy_entry_without_fed_at_passes_through():
    """Deploy-time safety: an entry already in the cache from a
    pre-TTL run (or a future code path that doesn't stamp) shouldn't
    be silently dropped. Read returns it; next feed will stamp it
    fresh."""
    runner = _make_runner({"catalyst_ttl_secs": 60})
    runner._catalyst_cache["AAPL"] = {"type": "fda", "score": 3}
    # No _fed_at key
    entry = runner._catalyst_for("AAPL")
    assert entry is not None
    assert entry["type"] == "fda"


def test_non_dict_entry_returned_as_is():
    """Defensive: some other code path puts a non-dict in the cache.
    Don't crash — just return it."""
    runner = _make_runner()
    runner._catalyst_cache["AAPL"] = "weird_string"
    assert runner._catalyst_for("AAPL") == "weird_string"


# === 5. Empty / None feeds are no-ops ===


def test_empty_feed_does_nothing():
    runner = _make_runner()
    runner.feed_catalyst_data({})
    assert len(runner._catalyst_cache) == 0


def test_none_feed_does_nothing():
    runner = _make_runner()
    runner.feed_catalyst_data(None)
    assert len(runner._catalyst_cache) == 0


# === 6. Read sites use the TTL gate (anti-regression) ===


def test_read_sites_use_catalyst_for_not_direct_cache():
    """Lock the refactor: all production catalyst reads inside
    momentum_runner.py go through `_catalyst_for(symbol)`, not raw
    `_catalyst_cache.get(symbol)`. If a future change re-introduces
    a direct cache access, the TTL gate is bypassed for that path."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "bot" / "strategies" / "momentum_runner.py").read_text()
    # Direct accesses are OK inside the cache management methods themselves
    # (feed_catalyst_data, _catalyst_for, etc.) but NOT in scoring paths.
    # The simple check: count direct `_catalyst_cache.get` reads — they
    # should only appear inside _catalyst_for itself.
    lines = src.split("\n")
    in_catalyst_for = False
    in_feed = False
    direct_reads_outside = 0
    for i, line in enumerate(lines):
        if "def _catalyst_for" in line:
            in_catalyst_for = True
            continue
        if "def feed_catalyst_data" in line:
            in_feed = True
            continue
        if line.startswith("    def "):
            in_catalyst_for = False
            in_feed = False
        if (not in_catalyst_for and not in_feed
                and "_catalyst_cache.get(symbol)" in line):
            direct_reads_outside += 1
    assert direct_reads_outside == 0, (
        f"Found {direct_reads_outside} direct `_catalyst_cache.get(symbol)` "
        "reads outside _catalyst_for — TTL gate bypassed!"
    )
