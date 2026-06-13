"""Catalyst type as explicit field on news signals (Wave 4).

PR #210 wired NewsFeed → momentum_runner.feed_catalyst_data, with
`get_catalyst_map` classifying the catalyst type by RE-GREPPING the
signal's `reason` text:

  if "fda" in reason: ctype = "fda"
  elif any(k in reason for k in ("beat", "earnings", ...)): ctype = "earnings"
  ...
  else: ctype = "news"

That's fragile: if anything else changes the reason format (e.g. a
prefix change, abbreviation, internationalization, or even a typo),
classification regresses to default "news". momentum_runner doesn't
care which type it is for scoring (2 points either way unless sector
heat is layered) but the bucket labels ARE used elsewhere (dashboard,
logging, audit grep).

Fix: classify at source. `_score_article` now stamps
`signal["catalyst_type"]` directly from the article text via a
typed keyword bucket. `get_catalyst_map` prefers the explicit
field, falling back to the legacy keyword scan only for signals
that predate this change (in-memory cross-version safety).

These tests pin:
  1. _classify_catalyst_type returns the right bucket per keyword
  2. FDA wins over generic positive sentiment
  3. earnings keywords classify earnings
  4. upgrade keywords classify upgrade
  5. Unknown / generic positive → "news" default
  6. _score_article stamps catalyst_type on BUY signals
  7. SELL signals get None (no catalyst classification)
  8. get_catalyst_map prefers explicit field
  9. get_catalyst_map falls back to reason-grep on legacy signals
"""
from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta


def _make_feed():
    from bot.signals.news_feed import NewsFeed
    cfg = SimpleNamespace(polygon_api_key=None)
    return NewsFeed(cfg)


# === 1. Classifier returns the right bucket ===


def test_classifier_fda_approval():
    from bot.signals.news_feed import NewsFeed
    assert NewsFeed._classify_catalyst_type("fda approval for new drug") == "fda"
    assert NewsFeed._classify_catalyst_type("fda cleared the device") == "fda"
    assert NewsFeed._classify_catalyst_type("fda authorized phase 3") == "fda"


def test_classifier_earnings_beat():
    from bot.signals.news_feed import NewsFeed
    assert NewsFeed._classify_catalyst_type("beat estimates by a wide margin") == "earnings"
    assert NewsFeed._classify_catalyst_type("blowout quarter announced") == "earnings"
    assert NewsFeed._classify_catalyst_type("raises guidance") == "earnings"
    assert NewsFeed._classify_catalyst_type("record earnings reported") == "earnings"


def test_classifier_upgrade():
    from bot.signals.news_feed import NewsFeed
    assert NewsFeed._classify_catalyst_type("upgraded to buy by analyst") == "upgrade"
    assert NewsFeed._classify_catalyst_type("price target raised") == "upgrade"
    assert NewsFeed._classify_catalyst_type("buy rating reaffirmed") == "upgrade"


def test_classifier_fallback_to_news():
    from bot.signals.news_feed import NewsFeed
    assert NewsFeed._classify_catalyst_type("positive sentiment overall") == "news"
    assert NewsFeed._classify_catalyst_type("major contract win announced") == "news"
    assert NewsFeed._classify_catalyst_type("") == "news"


def test_classifier_fda_wins_over_generic():
    """FDA-related news should classify as 'fda', not get pulled to
    'earnings' even if both keywords are present in the same article."""
    from bot.signals.news_feed import NewsFeed
    assert NewsFeed._classify_catalyst_type(
        "fda approval announcement during earnings call"
    ) == "fda"


# === 2. _score_article stamps the field ===


def test_score_article_stamps_catalyst_type_on_buy():
    feed = _make_feed()
    article = {
        "title": "Company XYZ receives FDA approval",
        "description": "Approval came after positive phase 3 trials",
        "url": "http://example.com",
        "published": "",
    }
    sig = feed._score_article(article, "XYZ")
    assert sig is not None
    assert sig["action"] == "buy"
    assert sig["catalyst_type"] == "fda"


def test_score_article_stamps_earnings_type():
    feed = _make_feed()
    article = {
        "title": "MSFT beats estimates, raises guidance",
        "description": "Record revenue for the quarter",
        "url": "", "published": "",
    }
    sig = feed._score_article(article, "MSFT")
    assert sig is not None
    assert sig["catalyst_type"] == "earnings"


def test_score_article_sell_signal_has_no_catalyst_type():
    """SELL signals are bearish exits — they don't carry a catalyst
    classification because momentum_runner doesn't consume them."""
    feed = _make_feed()
    article = {
        "title": "XYZ SEC investigation announced",
        "description": "Fraud allegations and class action",
        "url": "", "published": "",
    }
    sig = feed._score_article(article, "XYZ")
    assert sig is not None
    assert sig["action"] == "sell"
    assert sig["catalyst_type"] is None


# === 3. get_catalyst_map prefers explicit field ===


def test_get_catalyst_map_uses_explicit_field():
    """If signal["catalyst_type"] is set, use it directly — don't re-grep."""
    feed = _make_feed()
    feed.signals_generated.append({
        "symbol": "XYZ",
        "action": "buy",
        "catalyst_score": 3,
        "catalyst_type": "fda",  # explicit
        # reason mentions "earnings" — but explicit field wins
        "reason": "NEWS [earnings beat]: misleading reason text",
        "published": datetime.now().isoformat(),
    })
    m = feed.get_catalyst_map()
    assert m["XYZ"]["type"] == "fda"


def test_get_catalyst_map_falls_back_to_reason_grep():
    """Legacy signal without catalyst_type → reason scan still works."""
    feed = _make_feed()
    feed.signals_generated.append({
        "symbol": "XYZ",
        "action": "buy",
        "catalyst_score": 3,
        # No catalyst_type field set
        "reason": "NEWS [upgrade to buy]: analyst raised target",
        "published": datetime.now().isoformat(),
    })
    m = feed.get_catalyst_map()
    assert m["XYZ"]["type"] == "upgrade"


def test_get_catalyst_map_explicit_news_type_preserved():
    """If the source stamped 'news' (correct, no specific bucket),
    don't accidentally upgrade it via the reason grep."""
    feed = _make_feed()
    feed.signals_generated.append({
        "symbol": "XYZ",
        "action": "buy",
        "catalyst_score": 3,
        "catalyst_type": "news",  # explicit, generic
        # Reason mentions "earnings" — reason-grep would say earnings,
        # but the source stamp wins
        "reason": "NEWS [contract win, raises hope of earnings]: foo",
        "published": datetime.now().isoformat(),
    })
    m = feed.get_catalyst_map()
    assert m["XYZ"]["type"] == "news"
