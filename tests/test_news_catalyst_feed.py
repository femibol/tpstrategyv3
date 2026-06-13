"""Wire-up: NewsFeed.get_catalyst_map → momentum_runner.feed_catalyst_data.

2026-06-13 audit found `momentum_runner` (30% allocation, PRIMARY equity
strategy) had been silent for 30 days because the engine never invoked
`feed_catalyst_data()`. The 10-pt score has Catalyst worth 0-3, and the
>30% daily-change wall flat-out rejects the most active runners unless a
catalyst record exists — both gates were always closed.

Fix: NewsFeed already classifies article keywords and produces BUY
signals with `catalyst_score`. New `get_catalyst_map()` projects those
into the {symbol: {type, score, ...}} shape `feed_catalyst_data`
expects, and the engine scanner cycle ferries it across once per cycle.

These tests pin:
  1. Recent BUY signals with score ≥ 2 land in the map
  2. SELL / weak / stale signals are excluded
  3. Type classification matches the strategy's accepted buckets
  4. Highest-scoring catalyst per symbol wins on dedup
  5. The map flows through to momentum_runner._catalyst_cache
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace


def _make_news_feed():
    from bot.signals.news_feed import NewsFeed
    cfg = SimpleNamespace(polygon_api_key=None)
    feed = NewsFeed(cfg)
    feed.signals_generated = []
    return feed


def _signal(symbol, action, score, reason, minutes_ago=5):
    published = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "symbol": symbol,
        "action": action,
        "catalyst_score": score,
        "reason": reason,
        "published": published,
        "strategy": "news_catalyst",
    }


# === 1. Inclusion / exclusion ===


def test_recent_buy_signal_included():
    feed = _make_news_feed()
    feed.signals_generated.append(_signal("AAPL", "buy", 3, "NEWS [contract win]: foo"))
    m = feed.get_catalyst_map()
    assert "AAPL" in m
    assert m["AAPL"]["score"] == 3


def test_sell_signal_excluded():
    """SELL signals are exit-side bearish news, not catalyst-side fuel."""
    feed = _make_news_feed()
    feed.signals_generated.append(_signal("AAPL", "sell", 3, "NEWS [downgrade]: foo"))
    m = feed.get_catalyst_map()
    assert m == {}


def test_weak_signal_below_min_score_excluded():
    """catalyst_score=1 is the 'minor catalyst' bucket — don't pollute the
    runner's score table with noise."""
    feed = _make_news_feed()
    feed.signals_generated.append(_signal("AAPL", "buy", 1, "NEWS [positive]: foo"))
    m = feed.get_catalyst_map(min_score=2)
    assert m == {}


def test_stale_signal_excluded():
    """News older than the lookback window must drop out — catalyst
    momentum decays within hours, day-old news shouldn't unlock symbols."""
    feed = _make_news_feed()
    feed.signals_generated.append(
        _signal("AAPL", "buy", 3, "NEWS [beat]: foo", minutes_ago=500)
    )
    m = feed.get_catalyst_map(lookback_minutes=240)
    assert m == {}


# === 2. Type classification ===


def test_fda_keyword_classified_as_fda():
    feed = _make_news_feed()
    feed.signals_generated.append(_signal("BIOX", "buy", 3, "NEWS [fda approval]: blah"))
    m = feed.get_catalyst_map()
    assert m["BIOX"]["type"] == "fda"


def test_earnings_keyword_classified_as_earnings():
    feed = _make_news_feed()
    feed.signals_generated.append(_signal("MSFT", "buy", 3, "NEWS [beat estimates]: blah"))
    m = feed.get_catalyst_map()
    assert m["MSFT"]["type"] == "earnings"


def test_upgrade_keyword_classified_as_upgrade():
    feed = _make_news_feed()
    feed.signals_generated.append(_signal("NVDA", "buy", 2, "NEWS [upgrade to buy]: blah"))
    m = feed.get_catalyst_map()
    assert m["NVDA"]["type"] == "upgrade"


def test_default_classification_is_news():
    feed = _make_news_feed()
    feed.signals_generated.append(_signal("TSLA", "buy", 3, "NEWS [partnership]: blah"))
    m = feed.get_catalyst_map()
    assert m["TSLA"]["type"] == "news"


# === 3. Dedup keeps strongest ===


def test_highest_score_wins_on_dedup():
    """Two BUY signals on same ticker (e.g. earnings + an upgrade landed
    in same window) — keep the higher-scoring one."""
    feed = _make_news_feed()
    feed.signals_generated.append(_signal("AAPL", "buy", 2, "NEWS [upgrade]: x"))
    feed.signals_generated.append(_signal("AAPL", "buy", 5, "NEWS [beat estimates]: y"))
    m = feed.get_catalyst_map()
    assert m["AAPL"]["score"] == 5
    assert m["AAPL"]["type"] == "earnings"


# === 4. End-to-end into momentum_runner ===


def test_catalyst_map_flows_into_runner_cache():
    """The whole point: map produced here must be consumable by
    `MomentumRunnerStrategy.feed_catalyst_data` and land in `_catalyst_cache`
    with the type key the scorer reads at line 232 of the strategy."""
    from bot.strategies.momentum_runner import MomentumRunnerStrategy
    from bot.data.indicators import TechnicalIndicators

    feed = _make_news_feed()
    feed.signals_generated.append(_signal("XYZ", "buy", 3, "NEWS [fda cleared]: foo"))
    catalyst_map = feed.get_catalyst_map()

    runner = MomentumRunnerStrategy(
        {"min_score": 6, "min_price": 1, "max_price": 100,
         "min_volume": 100000, "max_daily_change_pct": 30,
         "max_open_positions": 5, "atr_stop_multiplier": 1.0},
        TechnicalIndicators(),
        capital=10000,
    )
    runner.feed_catalyst_data(catalyst_map)

    cached = runner._catalyst_cache.get("XYZ")
    assert cached is not None
    assert cached["type"] == "fda"
    assert cached["type"] in ("earnings", "news", "upgrade", "fda", "geopolitical"), (
        "type must match the bucket the strategy's score table reads"
    )
