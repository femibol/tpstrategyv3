"""
News Feed Signal Source - Real market news from News API.

Monitors breaking news for tracked stocks and politicians.
Generates signals based on significant news events.
"""
import time
import threading
from datetime import datetime, timedelta

import requests

from bot.utils.logger import get_logger

log = get_logger("signals.news_feed")

# Keywords that suggest bullish/bearish sentiment
BULLISH_KEYWORDS = [
    "upgrade", "beat", "exceeds expectations", "record revenue",
    "raised guidance", "strong earnings", "buy rating", "outperform",
    "partnership", "contract win", "FDA approval", "breakthrough",
    "acquisition", "dividend increase", "stock buyback", "all-time high",
]

BEARISH_KEYWORDS = [
    "downgrade", "miss", "below expectations", "warning",
    "cut guidance", "weak", "sell rating", "underperform",
    "lawsuit", "recall", "investigation", "SEC", "layoffs",
    "bankruptcy", "debt", "loss widens", "decline",
]


class NewsFeed:
    """
    Real news feed that monitors financial news for trading signals.

    Uses News API (newsapi.org) for real headlines.
    """

    def __init__(self, config, callback=None):
        self.config = config
        self.callback = callback
        self.api_key = config.news_api_key
        self.poll_interval = 600  # 10 minutes
        self._running = False
        self._thread = None

        # Track seen articles to avoid duplicates
        self.seen_articles = set()
        self.recent_news = []
        self.signals_generated = []

        # Symbols to monitor (pulled from strategies)
        self.watched_symbols = [
            "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
            "AMD", "SPY", "QQQ", "AB",  # AB = Pelosi's recent pick
        ]

    def start(self):
        """Start the news feed in a background thread."""
        if not self.api_key:
            log.warning("News API key not configured - news feed disabled")
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info("News feed started")

    def stop(self):
        self._running = False

    def _poll_loop(self):
        self._check_news()
        while self._running:
            time.sleep(self.poll_interval)
            if self._running:
                self._check_news()

    def _check_news(self):
        """Fetch and analyze latest financial news."""
        if not self.api_key:
            return

        log.info("Checking financial news...")

        # Fetch general market news
        articles = self._fetch_news()
        if not articles:
            return

        new_count = 0
        for article in articles:
            article_id = article.get("url", "")
            if article_id in self.seen_articles:
                continue

            self.seen_articles.add(article_id)
            self.recent_news.append(article)
            new_count += 1

            # Check if article mentions a watched symbol
            signal = self._analyze_article(article)
            if signal:
                self.signals_generated.append(signal)
                if self.callback:
                    self.callback(signal)

        if new_count > 0:
            log.info(f"Processed {new_count} new articles")

        # Keep last 200 articles
        if len(self.recent_news) > 200:
            self.recent_news = self.recent_news[-200:]

    def _fetch_news(self):
        """Fetch news from News API."""
        articles = []

        # Market news
        try:
            url = "https://newsapi.org/v2/top-headlines"
            params = {
                "apiKey": self.api_key,
                "country": "us",
                "category": "business",
                "pageSize": 30,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                articles.extend(data.get("articles", []))
        except Exception as e:
            log.debug(f"News API top headlines error: {e}")

        # Search for specific stock tickers
        for symbol in self.watched_symbols[:5]:  # Top 5 to avoid rate limits
            try:
                url = "https://newsapi.org/v2/everything"
                params = {
                    "apiKey": self.api_key,
                    "q": f"{symbol} stock",
                    "sortBy": "publishedAt",
                    "pageSize": 5,
                    "from": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                    "language": "en",
                }
                resp = requests.get(url, params=params, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for article in data.get("articles", []):
                        article["_matched_symbol"] = symbol
                        articles.append(article)
            except Exception as e:
                log.debug(f"News API search error for {symbol}: {e}")

        # Search for politician trading news
        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "apiKey": self.api_key,
                "q": "congress stock trading OR pelosi stock OR politician trades",
                "sortBy": "publishedAt",
                "pageSize": 10,
                "from": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"),
                "language": "en",
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for article in data.get("articles", []):
                    article["_type"] = "politician"
                    articles.append(article)
        except Exception as e:
            log.debug(f"News API politician search error: {e}")

        return articles

    def _analyze_article(self, article):
        """Analyze a news article for trading signals."""
        title = (article.get("title") or "").lower()
        description = (article.get("description") or "").lower()
        content = f"{title} {description}"

        # Find which symbol this is about
        matched_symbol = article.get("_matched_symbol")
        if not matched_symbol:
            for symbol in self.watched_symbols:
                if symbol.lower() in content or symbol in (article.get("title") or ""):
                    matched_symbol = symbol
                    break

        if not matched_symbol:
            return None

        # Sentiment analysis (simple keyword-based)
        bullish_score = sum(1 for kw in BULLISH_KEYWORDS if kw in content)
        bearish_score = sum(1 for kw in BEARISH_KEYWORDS if kw in content)

        if bullish_score == 0 and bearish_score == 0:
            return None  # Neutral - no signal

        if bullish_score > bearish_score:
            action = "buy"
            confidence = min(0.7, 0.4 + bullish_score * 0.1)
        elif bearish_score > bullish_score:
            action = "sell"
            confidence = min(0.7, 0.4 + bearish_score * 0.1)
        else:
            return None  # Mixed - no signal

        signal = {
            "symbol": matched_symbol.upper(),
            "action": action,
            "confidence": confidence,
            "source": "news_feed",
            "strategy": "news_sentiment",
            "reason": f"News: {article.get('title', '')[:100]}",
            "article_url": article.get("url", ""),
            "published": article.get("publishedAt", ""),
        }

        if action == "sell":
            signal["source"] = "exit"

        log.info(f"NEWS SIGNAL: {action.upper()} {matched_symbol} | {article.get('title', '')[:60]}")
        return signal

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
            "watched_symbols": self.watched_symbols,
        }
