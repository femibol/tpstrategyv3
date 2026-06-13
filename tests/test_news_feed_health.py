"""News feed health visibility (Wave 5).

PR #210 wired NewsFeed → momentum_runner so the catalyst score
component finally engaged. But if Polygon goes silent (API outage,
rate limit, news_feed thread crash, expired key), the engine still
calls `get_catalyst_map()`, gets an empty dict, and silently scores
catalyst=0. momentum_runner regresses to its pre-#210 30-day-silent
state and the operator has no signal.

Fix: track per-fetch success/error state. `is_healthy()` returns
(bool, age_secs, reason) so the engine can log a periodic WARNING
+ page the user when the feed goes silent for >30 minutes.

These tests pin:
  1. `last_successful_fetch` stamped on successful fetch
  2. `consecutive_fetch_errors` increments + resets correctly
  3. `is_healthy` returns True after recent success
  4. `is_healthy` returns False on stale-after threshold
  5. `is_healthy` returns False after 3 consecutive errors
  6. `is_healthy` returns False on not-started
  7. ibkr-only mode (no polygon) returns healthy + "ibkr_only" reason
  8. `get_status` includes the new health fields
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_feed(polygon_key=None):
    from bot.signals.news_feed import NewsFeed
    cfg = SimpleNamespace(polygon_api_key=polygon_key or "")
    feed = NewsFeed(cfg)
    return feed


# === 1. Successful fetch updates state ===


def test_successful_fetch_stamps_timestamp():
    feed = _make_feed()
    # Simulate the success path inside _check_news
    feed._client = MagicMock()  # pretend polygon configured
    feed._fetch_news = MagicMock(return_value=[])  # 0 articles is still success
    before = time.time()
    feed._check_news()
    after = time.time()
    assert feed._last_successful_fetch is not None
    assert before <= feed._last_successful_fetch <= after


def test_successful_fetch_resets_error_state():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._consecutive_fetch_errors = 5
    feed._last_fetch_error = "old error"
    feed._fetch_news = MagicMock(return_value=[])
    feed._check_news()
    assert feed._consecutive_fetch_errors == 0
    assert feed._last_fetch_error is None


def test_fetch_exception_increments_error_counter():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._fetch_news = MagicMock(side_effect=RuntimeError("api down"))
    feed._check_news()
    assert feed._consecutive_fetch_errors == 1
    assert feed._total_fetch_errors == 1
    assert "api down" in feed._last_fetch_error
    assert feed._last_successful_fetch is None


def test_consecutive_errors_accumulate():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._fetch_news = MagicMock(side_effect=RuntimeError("down"))
    for _ in range(3):
        feed._check_news()
    assert feed._consecutive_fetch_errors == 3
    assert feed._total_fetch_errors == 3


# === 2. is_healthy ===


def test_healthy_after_recent_success():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._running = True
    feed._last_successful_fetch = time.time() - 10  # 10s ago
    healthy, age, reason = feed.is_healthy(stale_after_secs=1800)
    assert healthy is True
    assert age < 30
    assert reason == "ok"


def test_unhealthy_when_stale():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._running = True
    feed._last_successful_fetch = time.time() - 3600  # 1h ago > 30min default
    healthy, age, reason = feed.is_healthy(stale_after_secs=1800)
    assert healthy is False
    assert reason.startswith("stale_")
    assert age > 1800


def test_unhealthy_after_three_consecutive_errors():
    """Even if the last success was recent, 3 errors in a row signals
    something is wrong NOW."""
    feed = _make_feed()
    feed._client = MagicMock()
    feed._running = True
    feed._last_successful_fetch = time.time() - 60  # recent
    feed._consecutive_fetch_errors = 3
    healthy, age, reason = feed.is_healthy()
    assert healthy is False
    assert "errors_3" in reason


def test_unhealthy_when_not_started():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._running = False
    healthy, age, reason = feed.is_healthy()
    assert healthy is False
    assert reason == "not_started"


def test_ibkr_only_mode_returns_healthy():
    """No polygon client → polling health isn't relevant. IBKR realtime
    news ticks can still flow even without the polling thread."""
    feed = _make_feed(polygon_key=None)
    feed._client = None
    healthy, age, reason = feed.is_healthy()
    assert healthy is True
    assert reason == "ibkr_only"


def test_first_poll_not_yet_succeeded_returns_unhealthy():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._running = True
    feed._last_successful_fetch = None
    feed._last_fetch_attempt = time.time()  # tried but no success yet
    healthy, age, reason = feed.is_healthy()
    assert healthy is False
    assert reason == "no_success_yet"


# === 3. get_status exposes health ===


def test_get_status_includes_health_fields():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._running = True
    feed._last_successful_fetch = time.time() - 30
    feed._consecutive_fetch_errors = 0
    feed._total_fetches = 100
    feed._total_fetch_errors = 5
    status = feed.get_status()
    assert "healthy" in status
    assert status["healthy"] is True
    assert "last_fetch_age_secs" in status
    assert status["last_fetch_age_secs"] is not None
    assert "health_reason" in status
    assert "consecutive_errors" in status
    assert status["total_fetches"] == 100
    assert status["total_fetch_errors"] == 5


def test_get_status_health_false_when_stale():
    feed = _make_feed()
    feed._client = MagicMock()
    feed._running = True
    feed._last_successful_fetch = time.time() - 99999
    status = feed.get_status()
    assert status["healthy"] is False


# === 4. Engine wiring (anti-regression) ===


def test_engine_calls_is_healthy_in_catalyst_block():
    """Lock the engine→is_healthy wiring. If a future PR drops the
    health check, news outages go silent again."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    catalyst_block_idx = src.find('"News: fed')
    assert catalyst_block_idx > 0, "catalyst feed block not found"
    # is_healthy must be called within ~50 lines of the catalyst feed
    next_section = src[catalyst_block_idx:catalyst_block_idx + 4000]
    assert "is_healthy" in next_section, (
        "engine.py catalyst block missing news_feed.is_healthy() check"
    )
    assert "NEWS FEED UNHEALTHY" in next_section, (
        "engine.py missing operator-visible WARNING for unhealthy feed"
    )
