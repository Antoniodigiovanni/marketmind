"""Keyless per-company news fetchers (Google News + Yahoo Finance RSS)."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import quote

import feedparser
import requests

USER_AGENT = "marketmind/0.1 (news ingest; free data only)"
GOOGLE_NEWS_FEED_URL = "https://news.google.com/rss/search"
YAHOO_FINANCE_FEED_URL = "https://finance.yahoo.com/rss/headline"


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Article:
    """A single news item fetched for one ticker."""

    ticker: str
    url: str
    title: str
    summary: str
    source: str  # 'google-news' | 'yahoo-finance'
    published_at: str  # ISO 8601 UTC

    @property
    def url_hash(self) -> str:
        """Return the dedup fingerprint of the article URL."""
        return _sha256(self.url)

    @property
    def content_hash(self) -> str:
        """Return the dedup fingerprint of the normalised title + summary."""
        body = re.sub(r"\s+", " ", f"{self.title} {self.summary}".strip().lower())
        return _sha256(body)


def google_news_url(ticker: str, name: str) -> str:
    """Build the Google News RSS URL for a ticker + company name query."""
    query = quote(f'"{ticker}" OR "{name}"')
    return f"{GOOGLE_NEWS_FEED_URL}?q={query}&hl=en-US&gl=US&ceid=US:en"


def yahoo_finance_url(symbol: str) -> str:
    """Build the Yahoo Finance RSS headline URL for a symbol."""
    return f"{YAHOO_FINANCE_FEED_URL}?s={quote(symbol)}"


def _published_iso(entry: feedparser.FeedParserDict) -> str | None:
    """Convert a feed entry's publish time to ISO 8601 UTC, if present."""
    parsed = getattr(entry, "published_parsed", None)
    if parsed is None:
        return None
    return dt.datetime(*parsed[:6], tzinfo=dt.UTC).isoformat()


def parse_feed(
    content: bytes,
    source: str,
    ticker: str,
    limit: int,
) -> list[Article]:
    """Parse RSS XML bytes into Article objects.

    Args:
        content: Raw RSS/Atom bytes.
        source: Feed source label ('google-news' | 'yahoo-finance').
        ticker: Ticker the feed was fetched for.
        limit: Maximum entries to take from the feed.

    Returns:
        List of Article objects with non-empty url and title.
    """
    feed = feedparser.parse(content)
    articles: list[Article] = []
    for entry in feed.entries[:limit]:
        url = getattr(entry, "link", "")
        title = getattr(entry, "title", "")
        if not url or not title:
            continue
        summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
        published_at = _published_iso(entry) or dt.datetime.now(dt.UTC).isoformat()
        articles.append(
            Article(
                ticker=ticker,
                url=url,
                title=title,
                summary=summary,
                source=source,
                published_at=published_at,
            )
        )
    return articles


def fetch_company_news(
    ticker: str,
    name: str,
    *,
    session: requests.Session | None = None,
    max_per_feed: int = 8,
    timeout: int = 20,
) -> list[Article]:
    """Fetch news for one company from Google News and Yahoo Finance.

    Feeds are fetched independently; a failure in one feed is swallowed so the
    other feed still contributes. Articles are deduplicated by url across feeds.

    Args:
        ticker: Yahoo-style ticker symbol.
        name: Company display name for the Google News query.
        session: Optional requests session (injected in tests).
        max_per_feed: Max entries taken per feed.
        timeout: HTTP timeout in seconds.

    Returns:
        De-duplicated list of Article objects.
    """
    http = session or requests.Session()
    feeds = [
        (google_news_url(ticker, name), "google-news"),
        (yahoo_finance_url(ticker), "yahoo-finance"),
    ]
    seen: set[str] = set()
    articles: list[Article] = []
    for url, source in feeds:
        try:
            response = http.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException:
            continue
        for article in parse_feed(response.content, source, ticker, max_per_feed):
            if article.url_hash in seen:
                continue
            seen.add(article.url_hash)
            articles.append(article)
    return articles
