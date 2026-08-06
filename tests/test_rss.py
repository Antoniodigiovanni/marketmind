"""Tests for the keyless per-company RSS news fetchers."""

from __future__ import annotations

import requests

from data.sources.rss import (
    Article,
    fetch_company_news,
    google_news_url,
    parse_feed,
    yahoo_finance_url,
)

RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>feed</title>{items}</channel></rss>"""

ITEM = """<item>
<title>{title}</title>
<link>{link}</link>
<description>{summary}</description>
<pubDate>Tue, 04 Aug 2026 09:00:00 GMT</pubDate>
</item>"""


def _feed_bytes(*links: str) -> bytes:
    items = "".join(
        ITEM.format(title=f"title-{i}", link=link, summary=f"summary-{i}")
        for i, link in enumerate(links)
    )
    return RSS_TEMPLATE.format(items=items).encode("utf-8")


class _FakeSession:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses

    def get(self, url: str, **kwargs) -> requests.Response:
        if url not in self.responses:
            raise requests.exceptions.ConnectionError(url)
        response = requests.Response()
        response.status_code = 200
        response._content = self.responses[url]
        return response


def test_google_news_url_quotes_ticker_and_name() -> None:
    url = google_news_url("AAPL", "Apple Inc")

    assert "news.google.com/rss/search" in url
    assert "%22AAPL%22" in url
    assert "%22Apple%20Inc%22" in url


def test_yahoo_finance_url_contains_symbol() -> None:
    url = yahoo_finance_url("SAP.DE")

    assert "finance.yahoo.com/rss/headline" in url
    assert "s=SAP.DE" in url


def test_parse_feed_extracts_article_fields() -> None:
    articles = parse_feed(
        _feed_bytes("https://example.com/1", "https://example.com/2"),
        "google-news",
        "AAPL",
        limit=10,
    )

    assert len(articles) == 2
    assert articles[0].ticker == "AAPL"
    assert articles[0].source == "google-news"
    assert articles[0].url == "https://example.com/1"
    assert articles[0].title == "title-0"
    assert articles[0].published_at.endswith("+00:00")


def test_parse_feed_honours_limit() -> None:
    articles = parse_feed(
        _feed_bytes("https://example.com/1", "https://example.com/2"),
        "google-news",
        "AAPL",
        limit=1,
    )

    assert len(articles) == 1


def test_fetch_company_news_combines_feeds_and_deduplicates_urls() -> None:
    shared = "https://example.com/shared"
    session = _FakeSession(
        {
            google_news_url("AAPL", "Apple Inc"): _feed_bytes(shared, "https://example.com/g1"),
            yahoo_finance_url("AAPL"): _feed_bytes(shared, "https://example.com/y1"),
        }
    )

    articles = fetch_company_news("AAPL", "Apple Inc", session=session, max_per_feed=10)

    assert len(articles) == 3
    assert {a.url for a in articles} == {shared, "https://example.com/g1", "https://example.com/y1"}
    assert {a.source for a in articles} == {"google-news", "yahoo-finance"}


def test_fetch_company_news_skips_failed_feed() -> None:
    session = _FakeSession({yahoo_finance_url("AAPL"): _feed_bytes("https://example.com/y1")})

    articles = fetch_company_news("AAPL", "Apple Inc", session=session, max_per_feed=10)

    assert [a.url for a in articles] == ["https://example.com/y1"]


def test_article_hashes_are_deterministic() -> None:
    a = Article(
        ticker="AAPL",
        url="https://example.com/x",
        title="Apple  raised  guidance",
        summary="Q3 revenue above consensus.",
        source="google-news",
        published_at="2026-08-04T09:00:00+00:00",
    )
    b = Article(
        ticker="AAPL",
        url="https://example.com/x",
        title="Apple raised guidance",
        summary="Q3 revenue above consensus.",
        source="google-news",
        published_at="2026-08-04T09:00:00+00:00",
    )

    assert a.url_hash == b.url_hash
    assert a.content_hash == b.content_hash
