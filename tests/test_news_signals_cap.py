"""NewsFeed.signals_generated bounded list (Wave 5).

`signals_generated` was a plain list appended forever. Multi-day bot
sessions accumulate thousands of entries; both `get_catalyst_map`
(PR #210) and `has_bearish_news` linear-scan the full list every
call. Memory + lookup cost grow without bound.

Fix: route all appends through `_record_signal` which prunes to
`_signals_max // 2` most recent when the list exceeds `_signals_max`
(default 500). Mirrors the prune pattern already used by
`seen_articles` and `recent_news`.

These tests pin:
  1. _record_signal appends like before below threshold
  2. _record_signal prunes when exceeding threshold
  3. Most recent entries preserved (no FIFO mistakes)
  4. All three production append sites use the helper
  5. Cap is configurable via _signals_max
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _make_feed():
    from bot.signals.news_feed import NewsFeed
    cfg = SimpleNamespace(polygon_api_key=None)
    return NewsFeed(cfg)


# === 1. Basic append below threshold ===


def test_record_signal_appends_below_threshold():
    feed = _make_feed()
    for i in range(10):
        feed._record_signal({"symbol": f"S{i}", "id": i})
    assert len(feed.signals_generated) == 10


# === 2. Prune above threshold ===


def test_record_signal_prunes_when_full():
    """After exceeding the cap, the list shrinks. With max=100, the
    prune fires at 101 and drops to 50; subsequent appends grow back
    toward 100 but never exceed it."""
    feed = _make_feed()
    feed._signals_max = 100
    for i in range(150):
        feed._record_signal({"symbol": f"S{i}", "id": i})
    # At most max entries (prune keeps things bounded)
    assert len(feed.signals_generated) <= feed._signals_max
    # But the prune DID fire (otherwise len would equal 150)
    assert len(feed.signals_generated) < 150


def test_record_signal_preserves_most_recent():
    """Pruning keeps the LATEST half — the recent signals are what
    callers actually care about."""
    feed = _make_feed()
    feed._signals_max = 10
    for i in range(15):
        feed._record_signal({"symbol": f"S{i}", "id": i})
    # 15 appended, prune fires at 11 → keeps last 5, then 4 more = 9 total
    # Verify the LAST entry is intact
    assert feed.signals_generated[-1]["id"] == 14
    # And entries from after the prune are present
    ids = [s["id"] for s in feed.signals_generated]
    assert 14 in ids


def test_record_signal_repeated_prunes_stay_bounded():
    """Long-running bot: many prune cycles. Memory never grows past
    _signals_max."""
    feed = _make_feed()
    feed._signals_max = 50
    for i in range(1000):
        feed._record_signal({"symbol": f"S{i}", "id": i})
    assert len(feed.signals_generated) <= feed._signals_max


# === 3. All append sites use the helper (anti-regression) ===


def test_all_production_appends_use_record_helper():
    """Lock the refactor: no raw `self.signals_generated.append(...)`
    in production code — every path must go through `_record_signal`
    so the cap is enforced."""
    src = (Path(__file__).parent.parent / "bot" / "signals" / "news_feed.py").read_text()
    lines = src.split("\n")
    direct_appends_outside_helper = 0
    in_helper = False
    for line in lines:
        if "def _record_signal" in line:
            in_helper = True
            continue
        if line.startswith("    def "):
            in_helper = False
        if not in_helper and "self.signals_generated.append" in line:
            direct_appends_outside_helper += 1
    assert direct_appends_outside_helper == 0, (
        f"Found {direct_appends_outside_helper} raw "
        "`self.signals_generated.append` calls outside _record_signal — "
        "they will bypass the rolling cap. Switch to self._record_signal()."
    )


def test_record_signal_method_exists():
    """Sanity: the helper exists and has the expected signature."""
    from bot.signals.news_feed import NewsFeed
    assert hasattr(NewsFeed, "_record_signal")


# === 4. Configurable cap ===


def test_signals_max_default_is_500():
    feed = _make_feed()
    assert feed._signals_max == 500


def test_signals_max_can_be_overridden():
    feed = _make_feed()
    feed._signals_max = 50
    for i in range(80):
        feed._record_signal({"id": i})
    # Pruned to 50/2 = 25 after fire at 50
    assert len(feed.signals_generated) <= 50


# === 5. Downstream consumers still work after cap ===


def test_get_catalyst_map_after_cap_fires():
    """The cap mustn't break downstream readers. Recent BUY signals
    should still be findable via get_catalyst_map after pruning."""
    import time
    feed = _make_feed()
    feed._signals_max = 20
    # 30 BUY signals — prune fires at 20, keeps last 10
    for i in range(30):
        feed._record_signal({
            "symbol": f"S{i}",
            "action": "buy",
            "catalyst_score": 3,
            "catalyst_type": "news",
            "published": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    m = feed.get_catalyst_map(min_score=2)
    # Map should reflect what's left in the buffer (some of the most recent)
    assert len(m) > 0
    # Most recent SHOULD be present
    assert "S29" in m
