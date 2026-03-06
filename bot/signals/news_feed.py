"""
Polygon.io News-Driven Trading (v3 Official Client)
Scans real-time financial news for profitable catalysts.

Uses the official polygon-api-client library:
  - list_ticker_news() → Auto-paginated news with typed TickerNews models
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

from bot.utils.logger import get_logger

log = get_logger("signals.news")

try:
    from polygon import RESTClient
    from polygon.rest.models import TickerNews
    from polygon.exceptions import BadResponse
    HAS_POLYGON = True
except (ImportError, KeyError, Exception) as e:
    HAS_POLYGON = False
    RESTClient = None
    TickerNews = None
    BadResponse = Exception
    log.warning(f"polygon-api-client unavailable ({type(e).__name__}): news polling disabled, IBKR news still works")

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
    "complete response letter": 3, "crl": 3,
    "earnings miss": 3, "misses estimates": 3, "revenue miss": 3,
    "cut guidance": 3, "lowers guidance": 3, "withdrawn guidance": 3,
    "guidance concerns": 3, "guidance disappoints": 3, "weak outlook": 3,
    "lowers outlook": 3, "cuts forecast": 3, "below guidance": 3,
    "downgraded to sell": 3, "downgrade to sell": 3, "downgraded to underperform": 3,
    "massive layoffs": 3, "major recall": 3,
    "accounting irregularities": 3, "restatement": 3,
    "investigation against": 3, "announces investigation": 3,
    "class action": 3, "securities fraud": 3,
    # Dilution / Offerings — small-cap killers (SHMD pattern: convertible notes = instant -10%+)
    "convertible notes": 3, "convertible senior notes": 3,
    "secondary offering": 3, "stock offering": 3, "share offering": 3,
    "public offering": 3, "direct offering": 3, "registered direct": 3,
    "shelf registration": 3, "shelf offering": 3,
    "at-the-market offering": 3, "atm offering": 3, "atm program": 3,
    "equity offering": 3, "mixed shelf": 3,
    "private placement": 3, "pipe offering": 3,
    # Lawsuits / Legal (WAL pattern: sues Jefferies)
    "lawsuit": 3, "sues": 3, "sued": 3,
    "loan charge": 3, "charge-off": 3, "loan dispute": 3,
    # Short seller reports — usually devastating for small caps
    "short report": 3, "short seller": 3,
    "hindenburg": 3, "muddy waters": 3, "citron research": 3,
    "iceberg research": 3, "grizzly research": 3,

    # Score 2 — Moderate bearish
    "miss": 2, "below expectations": 2, "disappointing": 2,
    "weak guidance": 2, "cautious outlook": 2, "headwinds": 2,
    "slides more than": 2, "trading lower": 2, "plunges": 2, "tumbles": 2,
    "sell rating": 2, "underperform": 2, "underweight": 2,
    "price target cut": 2, "price target lowered": 2,
    "layoffs": 2, "restructuring": 2, "cost cutting": 2,
    "recall": 2, "litigation": 2, "investigation": 2,
    "debt concern": 2, "downgrade": 2,
    "insider selling": 2, "insider sale": 2,
    # Dilution-adjacent
    "dilution": 2, "dilutive": 2, "warrant exercise": 2,
    "reverse split": 2, "reverse stock split": 2,
    "debt offering": 2, "notes offering": 2,
    "raises capital": 2, "capital raise": 2,
    # Operational
    "contract terminated": 2, "contract lost": 2,
    "plant closure": 2, "facility closure": 2,
    "going concern": 2, "default": 2, "covenant breach": 2,
    "credit downgrade": 2, "import ban": 2, "sanctions": 2,
    "subpoena": 2, "fda warning": 2,

    # Score 1 — Minor bearish
    "decline": 1, "bearish": 1, "weakness": 1, "slowing": 1,
    "competition": 1, "market share loss": 1, "pressure": 1,
    "priced offering": 1, "offering priced": 1,
}


class NewsFeed:
    """
    Multi-source news-driven trading signal generator.

    Sources:
      1. Polygon.io (v3 client) — polls every 2 minutes for article news
      2. IBKR real-time news ticks — instant headlines via TWS connection

    Generates BUY/SELL signals based on catalyst scoring.
    """

    def __init__(self, config, callback=None, polygon_api_key=None, broker=None):
        self.config = config
        self.callback = callback
        self.broker = broker  # IBKRBroker instance for real-time news
        self.api_key = polygon_api_key or config.polygon_api_key
        self.poll_interval = 120  # 2 minutes
        self._running = False
        self._thread = None
        self._client = None

        # Track seen articles to avoid duplicates
        self.seen_articles = set()
        self.recent_news = []
        self.signals_generated = []

        # Dynamic watchlist — updated from engine's active symbols
        self.watched_symbols = set()

        if self.api_key and HAS_POLYGON:
            self._client = RESTClient(
                api_key=self.api_key,
                retries=2,
                trace=False,
            )

    def update_watchlist(self, symbols):
        """Update the symbols to monitor for news (called by engine each cycle)."""
        self.watched_symbols = set(symbols)

    def start(self):
        """Start the news feed: Polygon polling + IBKR real-time ticks."""
        sources = []

        if self._client:
            self._running = True
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()
            sources.append("Polygon")

        # Subscribe to IBKR real-time news ticks (instant headlines)
        if self.broker and hasattr(self.broker, 'subscribe_news'):
            if self.broker.is_connected():
                self.broker.subscribe_news(callback=self._handle_ibkr_news)
                sources.append("IBKR")

        if sources:
            self._running = True
            log.info(f"News scanner started ({' + '.join(sources)}) - scanning for catalysts")
        else:
            log.warning("No news sources configured - news trading disabled")

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
        if not self._client:
            return

        try:
            articles = self._fetch_news()
            if not articles:
                return

            new_count = 0
            signal_count = 0

            for article in articles:
                article_id = article.get("id", "") or article.get("url", "")
                if article_id in self.seen_articles:
                    continue

                self.seen_articles.add(article_id)
                new_count += 1

                # Store for dashboard
                self.recent_news.append({
                    "title": article.get("title", ""),
                    "description": article.get("description", ""),
                    "url": article.get("url", ""),
                    "published": article.get("published", ""),
                    "tickers": article.get("tickers", []),
                    "publisher": article.get("publisher", ""),
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
        """Fetch latest news using the official Polygon client's list_ticker_news()."""
        all_articles = []

        # 1. General market news (catches broad catalysts)
        try:
            for n in self._client.list_ticker_news(
                order="desc",
                limit=50,
                sort="published_utc",
            ):
                if TickerNews and isinstance(n, TickerNews):
                    all_articles.append(self._normalize_article(n))
                # Stop after 50 total for general news
                if len(all_articles) >= 50:
                    break
        except BadResponse as e:
            if "429" in str(e):
                log.debug("Polygon news rate limited")
            else:
                log.debug(f"Polygon news error: {e}")
        except Exception as e:
            log.debug(f"Polygon news fetch error: {e}")

        # 2. Ticker-specific news for symbols we hold or are watching
        priority_symbols = list(self.watched_symbols)[:10]
        for symbol in priority_symbols:
            try:
                count = 0
                for n in self._client.list_ticker_news(
                    ticker=symbol,
                    order="desc",
                    limit=10,
                    sort="published_utc",
                ):
                    if TickerNews and isinstance(n, TickerNews):
                        all_articles.append(self._normalize_article(n))
                    count += 1
                    if count >= 10:
                        break
            except BadResponse as e:
                if "429" in str(e):
                    break  # Stop if rate limited
            except Exception:
                continue

        return all_articles

    def _normalize_article(self, news_item):
        """Convert a TickerNews model object to a plain dict for processing."""
        tickers = []
        if hasattr(news_item, 'tickers') and news_item.tickers:
            tickers = list(news_item.tickers)

        publisher_name = ""
        if hasattr(news_item, 'publisher') and news_item.publisher:
            publisher_name = getattr(news_item.publisher, 'name', str(news_item.publisher))

        return {
            "id": getattr(news_item, 'id', "") or "",
            "title": getattr(news_item, 'title', "") or "",
            "description": getattr(news_item, 'description', "") or "",
            "url": getattr(news_item, 'article_url', "") or "",
            "published": getattr(news_item, 'published_utc', "") or "",
            "tickers": tickers,
            "publisher": publisher_name,
        }

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

        headline = (article.get("title") or "")[:80]
        reason_str = ", ".join(reasons) if reasons else "sentiment"

        signal = {
            "symbol": ticker.upper(),
            "action": action,
            "confidence": round(confidence, 2),
            "source": "news_feed" if action == "buy" else "exit",
            "strategy": "news_catalyst",
            "reason": f"NEWS [{reason_str}]: {headline}",
            "article_url": article.get("url", ""),
            "published": article.get("published", ""),
            "catalyst_score": bull_score if action == "buy" else bear_score,
        }

        log.info(
            f"NEWS SIGNAL: {action.upper()} {ticker} "
            f"(score={net_score}, conf={confidence:.0%}) | {headline[:60]}"
        )
        return signal

    # =========================================================================
    # IBKR Real-Time News Processing
    # =========================================================================

    def _handle_ibkr_news(self, tick):
        """
        Process an IBKR real-time news tick. Fires instantly when
        headlines arrive via TWS connection (no polling delay).
        """
        try:
            headline = tick.get('headline', '')
            symbol = tick.get('symbol', '')
            article_id = tick.get('article_id', '')
            provider = tick.get('provider', '')

            if not headline:
                return

            # Deduplicate
            key = f"ibkr:{article_id or headline[:60]}"
            if key in self.seen_articles:
                return
            self.seen_articles.add(key)

            # Store for dashboard
            self.recent_news.append({
                "title": headline,
                "description": "",
                "url": "",
                "published": "",
                "tickers": [symbol] if symbol else [],
                "publisher": provider,
                "source": "ibkr",
            })

            # Score the headline
            if symbol and len(symbol) <= 5 and "." not in symbol:
                article = {"title": headline, "description": "", "tickers": [symbol]}
                signal = self._score_article(article, symbol)
                if signal:
                    signal["source_detail"] = f"ibkr_{provider}"
                    self.signals_generated.append(signal)
                    if self.callback:
                        self.callback(signal)

        except Exception as e:
            log.debug(f"IBKR news processing error: {e}")

    # =========================================================================
    # Dashboard / Status Methods
    # =========================================================================

    def get_recent_news(self, limit=20):
        """Get recent news articles for dashboard."""
        return self.recent_news[-limit:]

    def get_signals(self, limit=10):
        """Get generated signals."""
        return self.signals_generated[-limit:]

    def has_bearish_news(self, symbol, lookback_minutes=30):
        """Check if a symbol has recent bearish news that should block entry.

        Returns (is_bearish, reason_str) tuple.
        Used by engine to prevent buying stocks with negative catalysts
        (e.g., store closures, impairment charges, bankruptcy, downgrades).

        Args:
            symbol: Ticker to check
            lookback_minutes: How far back to check (default 30 min)

        Returns:
            (bool, str): (True if bearish news found, description of bearish catalyst)
        """
        if not self.signals_generated:
            return False, ""

        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(minutes=lookback_minutes)

        for sig in reversed(self.signals_generated):
            sig_sym = sig.get("symbol", "")
            if sig_sym.upper() != symbol.upper():
                continue
            # Check if signal is recent enough
            sig_time = sig.get("published", "")
            if sig_time:
                try:
                    from dateutil.parser import parse as parse_dt
                    if parse_dt(sig_time).replace(tzinfo=None) < cutoff:
                        continue
                except Exception:
                    pass  # If we can't parse time, still check the signal

            if sig.get("action") == "sell" and sig.get("source") == "exit":
                reason = sig.get("reason", "bearish news")
                catalyst_score = sig.get("catalyst_score", 0)
                if catalyst_score >= 3:
                    return True, reason

        # Also check recent_news directly for strong bearish keywords
        # (catches cases where news was scored but didn't generate a signal)
        for article in reversed(self.recent_news[-50:]):
            tickers = article.get("tickers", [])
            if symbol.upper() not in [t.upper() for t in tickers]:
                continue

            title = (article.get("title") or "").lower()
            desc = (article.get("description") or "").lower()
            content = f"{title} {desc}"

            # Strong bearish keywords that should block entry
            strong_bearish = [
                "bankruptcy", "impairment", "store closure", "closing stores",
                "sec investigation", "fraud", "delisted", "going concern",
                "cut guidance", "miss estimates", "earnings miss",
                "downgrade to sell", "warns of losses", "withdrawal",
                # Guidance-specific (OLPX pattern: beat earnings, cut guidance)
                "guidance concerns", "guidance disappoints", "weak outlook",
                "lowers outlook", "cuts forecast", "below guidance",
                # Investigation/lawsuit (GEMI pattern, WAL pattern)
                "investigation against", "announces investigation",
                "class action", "securities fraud",
                "lawsuit", "sues", "sued", "litigation",
                "loan charge", "charge-off", "loan dispute",
                "subpoena",
                # Dilution / Offerings (SHMD pattern: convertible notes financing = dilution trap)
                "convertible notes", "convertible senior notes",
                "secondary offering", "stock offering", "share offering",
                "public offering", "direct offering", "registered direct",
                "shelf registration", "shelf offering", "mixed shelf",
                "at-the-market offering", "atm offering", "atm program",
                "equity offering", "private placement", "pipe offering",
                "dilution", "reverse split", "reverse stock split",
                "notes offering", "capital raise",
                # Short seller reports
                "short report", "short seller",
                "hindenburg", "muddy waters", "citron research",
                "iceberg research", "grizzly research",
                # FDA / Biotech
                "fda rejection", "fda denied", "fda warning",
                "clinical trial failed", "complete response letter",
                # Analyst downgrades
                "shares fall", "falls after", "drops after",
                # Price action bearish headlines
                "slides more than", "plunges", "tumbles",
            ]
            found = [kw for kw in strong_bearish if kw in content]
            if found:
                headline = (article.get("title") or "")[:80]
                return True, f"Bearish news: [{', '.join(found[:2])}] {headline}"

        return False, ""

    def get_status(self):
        sources = []
        if self._client:
            sources.append("polygon")
        if self.broker and hasattr(self.broker, 'subscribe_news') and self.broker.is_connected():
            sources.append("ibkr")
        return {
            "running": self._running,
            "api_configured": bool(self._client),
            "ibkr_news": "ibkr" in sources,
            "total_articles": len(self.recent_news),
            "total_signals": len(self.signals_generated),
            "watched_symbols": list(self.watched_symbols)[:20],
            "sources": sources,
        }
