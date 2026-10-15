"""Keyless per-company news fetchers (Google News + Yahoo Finance RSS)."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urlparse

import feedparser
import requests

USER_AGENT = "marketmind/0.1 (news ingest; free data only)"
GOOGLE_NEWS_FEED_URL = "https://news.google.com/rss/search"
YAHOO_FINANCE_FEED_URL = "https://finance.yahoo.com/rss/headline"
DEFAULT_MAX_PER_FEED = 30
MAX_GOOGLE_WINDOW_DAYS = 7
_PUBLICATION_ISO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):?[0-5][0-9])"
)
_PUBLICATION_RFC = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),[ \t]+)?"
    r"(?:0?[1-9]|[12][0-9]|3[01])[ \t]+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[ \t]+[0-9]{4}[ \t]+"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?[ \t]+"
    r"(?:[+-](?:[01][0-9]|2[0-3])[0-5][0-9]|UT|UTC|GMT|EST|EDT|CST|CDT|MST|MDT|PST|PDT)",
    re.IGNORECASE | re.ASCII,
)


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
    publisher: str | None = None

    @property
    def url_hash(self) -> str:
        """Return the dedup fingerprint of the article URL."""
        return _sha256(self.url)

    @property
    def content_hash(self) -> str:
        """Return the cross-feed dedup fingerprint of the normalized title."""
        return _sha256(normalize_title(self.title, publisher=self.publisher))


NEWS_RECORD_FIELDS: tuple[str, ...] = (
    "ticker",
    "url",
    "url_hash",
    "title",
    "summary",
    "source",
    "publisher",
    "published_at",
    "content_hash",
)


def article_to_record(article: Article) -> dict[str, str | None]:
    """Serialise an article into the raw NDJSON record stored under data/raw/news.

    Single source of truth for the on-disk news schema: the ingest job writes
    these fields and the MCP read tools query them. Any change here must be
    reflected in NEWS_RECORD_FIELDS, which server.mcp asserts against.

    Args:
        article: Fetched article.

    Returns:
        Dict keyed by NEWS_RECORD_FIELDS.
    """
    return {
        "ticker": article.ticker,
        "url": article.url,
        "url_hash": article.url_hash,
        "title": article.title,
        "summary": article.summary,
        "source": article.source,
        "publisher": article.publisher,
        "published_at": article.published_at,
        "content_hash": article.content_hash,
    }


def normalize_title(title: str, *, publisher: str | None = None) -> str:
    """Normalize a headline for cross-feed deduplication."""
    value = title.strip()
    if publisher:
        suffix = f" - {publisher}".casefold()
        if value.casefold().endswith(suffix):
            value = value[: -len(suffix)]
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r"\s+", " ", value).strip()


def google_news_url(ticker: str, name: str, *, window_days: int = 1) -> str:
    """Build a bounded Google News RSS URL for a company query."""
    if not 1 <= window_days <= MAX_GOOGLE_WINDOW_DAYS:
        raise ValueError(f"window_days must be between 1 and {MAX_GOOGLE_WINDOW_DAYS}")
    query = quote(f'("{ticker}" OR "{name}") when:{window_days}d')
    return f"{GOOGLE_NEWS_FEED_URL}?q={query}&hl=en-US&gl=US&ceid=US:en"


def yahoo_finance_url(symbol: str) -> str:
    """Build the Yahoo Finance RSS headline URL for a symbol."""
    return f"{YAHOO_FINANCE_FEED_URL}?s={quote(symbol)}"


def parse_publication_iso(value: str) -> dt.datetime | None:
    """Validate a supported ISO publication timestamp and normalize it to UTC.

    Args:
        value: Calendar timestamp with seconds and Z or a numeric HH:MM/HHMM zone.

    Returns:
        UTC datetime, or None for invalid or unsupported syntax.
    """
    if not isinstance(value, str) or _PUBLICATION_ISO.fullmatch(value) is None:
        return None
    try:
        return dt.datetime.fromisoformat(value).astimezone(dt.UTC)
    except (ValueError, OverflowError):
        return None


def _published_iso(entry: feedparser.FeedParserDict) -> str | None:
    """Validate the original publication timezone before converting to UTC."""
    raw = entry.get("published", "").strip()
    if not raw:
        return None
    # Feedparser's parsed tuples lose missing and unrecognized timezone information.
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}T", raw):
            published = parse_publication_iso(raw)
            return published.isoformat() if published is not None else None
        else:
            # Match every field so the email parser consumes the validated zone.
            # Extra tokens and comments are unsupported, even if it accepts them.
            rfc_match = _PUBLICATION_RFC.fullmatch(raw)
            if rfc_match is None:
                return None
            published = parsedate_to_datetime(rfc_match[0])
        if published.tzinfo is None or published.utcoffset() is None:
            return None
        return published.astimezone(dt.UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _publisher(entry: feedparser.FeedParserDict, url: str) -> str | None:
    """Return the publisher carried by the feed, with hostname fallback."""
    source = getattr(entry, "source", None)
    if source:
        title = source.get("title") if hasattr(source, "get") else None
        if title:
            return str(title).strip()
    hostname = urlparse(url).hostname
    if not hostname:
        return None
    return hostname.removeprefix("www.")


def _unwrap_google_url(url: str) -> str:
    """Extract an explicit publisher URL from a Google wrapper when present."""
    parsed = urlparse(url)
    if parsed.hostname not in {"news.google.com", "www.news.google.com"}:
        return url
    query = parse_qs(parsed.query)
    for key in ("url", "q"):
        candidate = query.get(key, [""])[0]
        if candidate.startswith(("https://", "http://")):
            return candidate
    return url


def parse_feed(
    content: bytes,
    source: str,
    ticker: str,
    limit: int,
    published_after: dt.datetime | None = None,
) -> list[Article]:
    """Parse RSS XML bytes into Article objects.

    Args:
        content: Raw RSS/Atom bytes.
        source: Feed source label ('google-news' | 'yahoo-finance').
        ticker: Ticker the feed was fetched for.
        limit: Maximum entries to take from the feed.
        published_after: Drop entries published at or before this high-water mark.

    Returns:
        List of Article objects with non-empty url and title.
    """
    feed = feedparser.parse(content)
    articles: list[Article] = []
    for entry in feed.entries:
        url = getattr(entry, "link", "")
        title = getattr(entry, "title", "")
        if not url or not title:
            continue
        url = _unwrap_google_url(url)
        summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
        # Without a publication time an entry cannot establish historical eligibility.
        published_at = _published_iso(entry)
        if published_at is None:
            continue
        published = dt.datetime.fromisoformat(published_at)
        if published_after is not None and published <= published_after:
            continue
        publisher = _publisher(entry, url)
        articles.append(
            Article(
                ticker=ticker,
                url=url,
                title=title,
                summary=summary,
                source=source,
                published_at=published_at,
                publisher=publisher,
            )
        )
        if len(articles) >= limit:
            break
    return articles


def fetch_company_news(
    ticker: str,
    name: str,
    *,
    session: requests.Session | None = None,
    max_per_feed: int = DEFAULT_MAX_PER_FEED,
    google_window_days: int = 1,
    published_after: dict[str, dt.datetime] | None = None,
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
        google_window_days: Bounded Google query window, from 1 to 7 days.
        published_after: Optional per-source high-water marks.
        timeout: HTTP timeout in seconds.

    Returns:
        De-duplicated list of Article objects.
    """
    http = session or requests.Session()
    high_water = published_after or {}
    feeds = [
        (google_news_url(ticker, name, window_days=google_window_days), "google-news"),
        (yahoo_finance_url(ticker), "yahoo-finance"),
    ]
    seen_urls: set[str] = set()
    seen_content: set[str] = set()
    articles: list[Article] = []
    for url, source in feeds:
        try:
            response = http.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException:
            continue
        for article in parse_feed(
            response.content,
            source,
            ticker,
            max_per_feed,
            published_after=high_water.get(source),
        ):
            if article.url_hash in seen_urls or article.content_hash in seen_content:
                continue
            seen_urls.add(article.url_hash)
            seen_content.add(article.content_hash)
            articles.append(article)
    return articles
