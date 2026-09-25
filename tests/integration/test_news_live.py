"""Live contract checks for the two production news feeds."""

from __future__ import annotations

import pytest
import requests

from data.sources.rss import USER_AGENT, google_news_url, parse_feed, yahoo_finance_url

pytestmark = pytest.mark.live


def _fetch_feed(url: str, source: str):
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    assert response.content.strip()
    articles = parse_feed(response.content, source, "AAPL", 5)
    assert articles
    assert all(item.url and item.title and item.published_at for item in articles)
    return articles


def test_live_google_news_rss_contract() -> None:
    """Parse Google's current company-news RSS schema."""
    articles = _fetch_feed(google_news_url("AAPL", "Apple Inc", window_days=7), "google-news")
    assert all(item.source == "google-news" for item in articles)


def test_live_yahoo_finance_rss_contract() -> None:
    """Parse Yahoo Finance's current ticker-news RSS schema."""
    articles = _fetch_feed(yahoo_finance_url("AAPL"), "yahoo-finance")
    assert all(item.source == "yahoo-finance" for item in articles)
