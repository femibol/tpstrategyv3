"""
Polygon.io News-Driven Trading - Scans real-time financial news for profitable catalysts.

Uses Polygon's /v2/reference/news endpoint which returns:
- Ticker-tagged articles (knows EXACTLY which stocks are mentioned)
- Real-time publishing (no 10-minute delay)
- Multiple news sources (Benzinga, GlobeNewsWire, etc.)

Generates actionable BUY/SELL signals based on catalyst scoring:
- FDA approvals, contract wins, earnings beats → BUY
- Downgrades, lawsuits, SEC investigations → SELL/EXIT
- Combines news sentiment with price action confirmation
"""
import time
import threading
from datetime import datetime, timedelta

import requests

from bot.utils.logger import get_logger

log = get_logger("signals.news")

# High-conviction catalyst keywords with impact scores
# Score 1-3: 1=minor, 2=moderate, 3=strong catalyst
BULLISH_CATALYSTS = {
    # Score 3 — Strong catalysts (high confidence, act fast)
    "fda approval": 3, "fda cleared": 3, "fda authorized": 3,
    "beat estimates": 3, "beats expectations": 3, "record revenue": 3,
    "record earnings": 3, "raised guidance": 3, "raises guidance": 3,
    "upgraded to buy": 3, "upgrade to buy": 3, "upgraded to outperform": 3,
    "contract award": 3, "contract win": 3, "major contract": 3,
    "acquisition of": 3, "to acquire": 3, "merger agreement": 3,
    "stock buyback": 3, "share repurchase": 3, "special dividend": 3,
    "all-time high": 3, "new all-time": 3,
    "short squeeze": 3, "heavily shorted": 3,

    # Score 2 — Moderate catalysts
    "beat": 2, "exceeds": 2, "surpasses": 2, "tops estimates": 2,
    "strong earnings": 2, "solid quarter": 2, "blowout quarter": 2,
    "revenue growth": 2, "profit growth": 2, "margin expansion": 2,
    "buy rating": 2, "outperform": 2, "overweight": 2, "price target raised": 2,
    "partnership": 2, "strategic alliance": 2, "collaboration": 2,
    "breakthrough": 2, "innovation": 2, "patent granted": 2,
    "dividend increase": 2, "dividend hike": 2,
    "insider buying": 2, "insider purchase": 2,
    "analyst upgrade": 2,

    # Score 1 — Minor catalysts (trade with confirmation)
    "positive": 1, "bullish": 1, "momentum": 1, "surge": 1, "rally": 1,
    "expansion": 1, "new product": 1, "product launch": 1,
    "market share": 1, "growing demand": 1,
}

BEARISH_CATALYSTS = {
    # Score 3 — Strong bearish catalysts (exit positions)
    "sec investigation": 3, "sec charges": 3, "fraud": 3,
    "bankruptcy": 3, "chapter 11": 3, "delisted": 3, "delisting": 3,
    "fda rejection": 3, "fda denied": 3, "clinical trial failed": 3,
    "earnings miss": 3, "misses estimates": 3, "revenue miss": 3,
    "cut guidance": 3, "lowers guidance": 3, "withdrawn guidance": 3,
    "downgraded to sell": 3, "downgrade to sell": 3, "downgraded to underperform": 3,
    "massive layoffs": 3, "major recall": 3,
    "accounting irregularities": 3, "restatement": 3,

    # Score 2 — Moderate bearish
    "miss": 2, "below expectations": 2, "disappointing": 2,
    "weak guidance": 2, "cautious outlook": 2, "headwinds": 2,
    "sell rating": 2, "underperform": 2, "underweight": 2,
    "price target cut": 2, "price target lowered": 2,
    "layoffs": 2, "restructuring": 2, "cost cutting": 2,
    "recall": 2, "lawsuit": 2, "litigation": 2,
    "debt concern": 2, "downgrade": 2,
    "insider selling": 2, "insider sale": 2,

    # Score 1 — Minor bearish
    "decline": 1, "bearish": 1, "weakness": 1, "slowing": 1,
    "competition": 1, "market share loss": 1, "pressure": 1,
}


class NewsFeed:
    """
    Polygon.io news-driven trading signal generator.

    Scans real-time news every 2 minutes, scores catalysts,
    and generates BUY/SELL signals for the engine to execute.
    """

    POLYGON_NEWS_URL = "https://api.polygon.io/v2/reference/news"

    def __init__(self, config, callback=None, polygon_api_key=None):
        self.config = config
        self.callback = callback
        self.api_key = polygon_api_key or config.polygon_api_key
        self.poll_interval = 120  # 2 minutes (was 10 min with old API)
        self._running = False
        self._thread = None

        # Track seen articles to avoid duplicates
        self.seen_articles = set()
        self.recent_news = []
        self.signals_generated = []

        # Dynamic watchlist — updated from engine's active symbols
        self.watched_symbols = set()

    def update_watchlist(self, symbols):
        """Update the symbols to monitor for news (called by engine each cycle)."""
        self.watched_symbols = set(symbols)

    def start(self):
        """Start the news feed in a background thread."""
        if not self.api_key:
            log.warning("Polygon API key not set — news trading disabled")
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info("Polygon news scanner started — scanning for catalysts every 2 min")

    def stop(self):
        self._running = False

    def _poll_loop(self):
        self._check_news()
        while self._running:
            time.sleep(self.poll_interval)
            if self._running:
                self._check_news()

    def _check_news(self):
        """Fetch and analyze latest news from Polygon."""
        if not self.api_key:
            return

        try:
            articles = self._fetch_news()
            if not articles:
                return

            new_count = 0
            signal_count = 0

            for article in articles:
                article_id = article.get("id", "") or article.get("article_url", "")
                if article_id in self.seen_articles:
                    continue

                self.seen_articles.add(article_id)
                new_count += 1

                # Store for dashboard
                self.recent_news.append({
                    "title": article.get("title", ""),
                    "description": article.get("description", ""),
                    "url": article.get("article_url", ""),
                    "published": article.get("published_utc", ""),
                    "tickers": article.get("tickers", []),
                    "publisher": article.get("publisher", {}).get("name", ""),
                    "source": "polygon",
                })

                # Analyze each ticker mentioned in the article
                tickers = article.get("tickers", [])
                for ticker in tickers:
                    if ticker and len(ticker) <= 5 and "." not in ticker:
                        signal = self._score_article(article, ticker)
                        if signal:
                            self.signals_generated.append(signal)
                            signal_count += 1
                            if self.callback:
                                self.callback(signal)

            if new_count > 0:
                log.info(f"News: {new_count} new articles → {signal_count} signals generated")

            # Keep last 500 articles
            if len(self.recent_news) > 500:
                self.recent_news = self.recent_news[-500:]

            # Prune old seen articles (keep last 2000)
            if len(self.seen_articles) > 2000:
                self.seen_articles = set(list(self.seen_articles)[-1000:])

        except Exception as e:
            log.warning(f"News check error: {e}")

    def _fetch_news(self):
        """Fetch latest news from Polygon /v2/reference/news."""
        all_articles = []

        # 1. General market news (catches broad catalysts)
        try:
            params = {
                "apiKey": self.api_key,
                "limit": 50,
                "sort": "published_utc",
                "order": "desc",
            }
            resp = requests.get(self.POLYGON_NEWS_URL, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                all_articles.extend(data.get("results", []))
            elif resp.status_code == 429:
                log.debug("Polygon news rate limited")
            else:
                log.debug(f"Polygon news: HTTP {resp.status_code}")
        except Exception as e:
            log.debug(f"Polygon news fetch error: {e}")

        # 2. Ticker-specific news for symbols we hold or are watching
        # Only check top priority symbols to stay within rate limits
        priority_symbols = list(self.watched_symbols)[:10]
        for symbol in priority_symbols:
            try:
                params = {
                    "apiKey": self.api_key,
                    "ticker": symbol,
                    "limit": 10,
                    "sort": "published_utc",
                    "order": "desc",
                }
                resp = requests.get(self.POLYGON_NEWS_URL, params=params, timeout=8)
                if resp.status_code == 200:
                    data = resp.json()
                    all_articles.extend(data.get("results", []))
                elif resp.status_code == 429:
                    break  # Stop if rate limited
            except Exception:
                continue

        return all_articles

    def _score_article(self, article, ticker):
        """
        Score a news article for a specific ticker.
        Returns a trading signal if conviction is high enough, else None.
        """
        title = (article.get("title") or "").lower()
        description = (article.get("description") or "").lower()
        content = f"{title} {description}"

        # Score bullish and bearish catalysts
        bull_score = 0
        bull_reasons = []
        for keyword, score in BULLISH_CATALYSTS.items():
            if keyword in content:
                bull_score += score
                if score >= 2:
                    bull_reasons.append(keyword)

        bear_score = 0
        bear_reasons = []
        for keyword, score in BEARISH_CATALYSTS.items():
            if keyword in content:
                bear_score += score
                if score >= 2:
                    bear_reasons.append(keyword)

        # Need clear directional bias — skip if mixed or weak
        if bull_score <= 1 and bear_score <= 1:
            return None  # Too weak to act on
        if bull_score > 0 and bear_score > 0 and abs(bull_score - bear_score) < 2:
            return None  # Mixed signals — skip

        # Determine action and confidence
        if bull_score > bear_score:
            action = "buy"
            net_score = bull_score - bear_score
            reasons = bull_reasons[:3]
            # Confidence: score 2-3 = 0.5, score 4-5 = 0.6, score 6+ = 0.7
            confidence = min(0.75, 0.4 + net_score * 0.07)
        elif bear_score > bull_score:
            action = "sell"
            net_score = bear_score - bull_score
            reasons = bear_reasons[:3]
            confidence = min(0.75, 0.4 + net_score * 0.07)
        else:
            return None

        # Minimum confidence threshold
        if confidence < 0.45:
            return None

        headline = article.get("title", "")[:80]
        reason_str = ", ".join(reasons) if reasons else "sentiment"

        signal = {
            "symbol": ticker.upper(),
            "action": action,
            "confidence": round(confidence, 2),
            "source": "news_feed" if action == "buy" else "exit",
            "strategy": "news_catalyst",
            "reason": f"NEWS [{reason_str}]: {headline}",
            "article_url": article.get("article_url", ""),
            "published": article.get("published_utc", ""),
            "catalyst_score": bull_score if action == "buy" else bear_score,
        }

        log.info(
            f"NEWS SIGNAL: {action.upper()} {ticker} "
            f"(score={net_score}, conf={confidence:.0%}) | {headline[:60]}"
        )
        return signal

    # =========================================================================
    # Dashboard / Status Methods
    # =========================================================================

    def get_recent_news(self, limit=20):
        """Get recent news articles for dashboard."""
        return self.recent_news[-limit:]

    def get_signals(self, limit=10):
        """Get generated signals."""
        return self.signals_generated[-limit:]

    def get_status(self):
        return {
            "running": self._running,
            "api_configured": bool(self.api_key),
            "total_articles": len(self.recent_news),
            "total_signals": len(self.signals_generated),
            "watched_symbols": list(self.watched_symbols)[:20],
            "source": "polygon",
        }
