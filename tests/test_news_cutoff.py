"""Offline publication boundary regressions, independent of database availability."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, Mock

import polars as pl
import pytest

from agent import ingestion
from data.sources.rss import Article, parse_feed


@pytest.mark.asyncio
@pytest.mark.parametrize("default_fetch", [False, True])
@pytest.mark.parametrize(
    "stored_checkpoints",
    [
        {"google-news": dt.datetime(2026, 9, 1, tzinfo=dt.UTC)},
        {"google-news": dt.datetime(2026, 8, 5, tzinfo=dt.UTC)},
        {"google-news": dt.datetime(2026, 8, 4, 9, tzinfo=dt.UTC)},
        {
            "google-news": dt.datetime(2026, 8, 4, 9, tzinfo=dt.UTC),
            "yahoo-finance": dt.datetime(2026, 8, 5, tzinfo=dt.UTC),
        },
    ],
)
@pytest.mark.parametrize(
    ("published_at", "eligible"),
    [
        ("2026-08-05T00:00:00+00:00", False),
        ("2026-08-10T12:00:00+00:00", False),
        ("2026-08-04T23:59:59+00:00", True),
        ("2026-08-05T01:00:00+02:00", True),
        ("2026-08-04T23:00:00-02:00", False),
        ("2026-08-04T12:00:00", False),
        ("", False),
        ("invalid", False),
        *[
            (f"2026-08-05T00:30:00{offset}", False)
            for offset in (
                "+00:60:00",
                "-00:60:00",
                "+00:00:60",
                "+006000",
                "+00:60",
                "-0060",
                "+24:00",
                "+01",
                "+01:00:00",
                "+010000",
                "+01:00.5",
                "+0100,5",
                "+01:00:00.5",
            )
        ],
        ("2026-08-04T23:00:00Z", True),
        ("2026-08-05T00:30:00+0100", True),
        ("2026-08-04T22:00:00-0100", True),
        ("2026-08-04T23:00:00.123456+00:00", True),
    ],
)
async def test_cutoff_precedes_all_article_side_effects(
    monkeypatch,
    tmp_path,
    published_at: str,
    eligible: bool,
    default_fetch: bool,
    stored_checkpoints: dict[str, dt.datetime],
) -> None:
    company_id = "company"
    for name in (
        "start_run",
        "finish_run",
        "get_current_profile",
        "register_article",
        "get_current_facts",
        "search_similar_facts",
        "set_article_status",
        "update_feed_checkpoints",
    ):
        monkeypatch.setattr(ingestion, name, AsyncMock())
    monkeypatch.setattr(ingestion, "_ensure_company", AsyncMock(return_value=(company_id, "")))
    monkeypatch.setattr(
        ingestion,
        "get_current_profile",
        AsyncMock(
            return_value={
                "description": "",
                "products_services": [],
            }
        ),
    )
    monkeypatch.setattr(
        ingestion,
        "get_feed_checkpoints",
        AsyncMock(return_value=stored_checkpoints),
    )
    article = Article(
        "A", "https://example.com/story", "Title", "Summary", "google-news", published_at
    )
    llm_calls = []
    score = Mock(wraps=ingestion.pre_score)
    monkeypatch.setattr(ingestion, "pre_score", score)

    def fetch(ticker: str, name: str, **kwargs: object) -> list[Article]:
        if default_fetch:
            assert kwargs["published_after"] == {
                source: value
                for source, value in stored_checkpoints.items()
                if value < dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
            }
            assert kwargs["google_window_days"] == (
                1 if "google-news" in kwargs["published_after"] else 7
            )
        return [article]

    monkeypatch.setattr(ingestion, "fetch_company_news", fetch)
    await ingestion.run_daily_ingest(
        None,
        "2026-08-04",
        universe=pl.DataFrame({"ticker": ["A"], "name": ["Company"]}),
        news_fetcher=None if default_fetch else fetch,
        llm=lambda messages: llm_calls.append(messages) or "REDUNDANT",
        raw_news_dir=tmp_path,
    )
    assert ingestion.register_article.await_count == int(eligible)
    assert len(llm_calls) == int(eligible)
    assert score.call_count == int(eligible)
    if not eligible:
        ingestion.get_current_facts.assert_not_awaited()
        ingestion.search_similar_facts.assert_not_awaited()
        ingestion.set_article_status.assert_not_awaited()
    path = tmp_path / "2026-08-04.ndjson"
    assert path.exists() == eligible
    checkpoints = ingestion.update_feed_checkpoints.call_args.kwargs["published_by_source"]
    assert bool(checkpoints) == eligible
    if eligible:
        expected = dt.datetime.fromisoformat(published_at).astimezone(dt.UTC)
        assert checkpoints == {"google-news": expected}


@pytest.mark.parametrize("publication", ["", "<pubDate>invalid</pubDate>"])
def test_rss_never_fabricates_publication(publication: str) -> None:
    content = (
        "<rss version='2.0'><channel><item><title>Title</title>"
        "<link>https://example.com/story</link>"
        f"{publication}</item></channel></rss>"
    ).encode()
    assert parse_feed(content, "google-news", "A", 10) == []


@pytest.mark.parametrize(
    "offset",
    [
        "+00:60:00",
        "-00:60:00",
        "+00:00:60",
        "+006000",
        "+00:60",
        "-0060",
        "+24:00",
        "+01",
        "+01:00:00",
        "+010000",
        "+01:00.5",
        "+0100,5",
        "+01:00:00.5",
    ],
)
def test_rss_rejects_unsupported_iso_offsets(offset: str) -> None:
    content = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Title</title>'
        '<link href="https://example.com/story"/>'
        f"<published>2026-08-05T00:30:00{offset}</published></entry></feed>"
    ).encode()
    assert parse_feed(content, "google-news", "A", 10) == []
