"""End-to-end tests for the daily company-level news → facts pipeline."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest

from agent.ingestion import run_daily_ingest
from agent.knowledge import get_current_aggregate_facts, get_current_facts, write_fact
from data.db.init import init_db
from data.sources.rss import Article

DATE = "2026-08-04"

UNIVERSE = pl.DataFrame(
    {
        "ticker": ["AAPL", "MSFT"],
        "name": ["Apple Inc", "Microsoft Corp"],
        "country": ["US", "US"],
        "sector": ["Technology", "Technology"],
        "added_date": [dt.date(2026, 1, 1), dt.date(2026, 1, 1)],
        "removed_date": [None, None],
        "universe": ["sp500", "sp500"],
    }
)


def _article(
    ticker: str,
    url: str,
    title: str,
    summary: str,
    source: str = "google-news",
) -> Article:
    return Article(
        ticker=ticker,
        url=url,
        title=title,
        summary=summary,
        source=source,
        published_at="2026-08-04T09:00:00+00:00",
    )


def _fake_news_fetcher(ticker: str, name: str) -> list[Article]:
    if ticker == "AAPL":
        return [
            _article(
                "AAPL",
                "https://example.com/aapl-1",
                "Apple raises Q3 guidance",
                "Revenue above consensus.",
            ),
            _article(
                "AAPL",
                "https://example.com/aapl-1",
                "Apple raises Q3 guidance",
                "Revenue above consensus.",
            ),
        ]
    return [
        _article(
            "MSFT",
            "https://example.com/msft-1",
            "Microsoft announces layoffs",
            "10k roles cut.",
        )
    ]


class _CountingDescriptionFetcher:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, name: str) -> str:
        self.calls += 1
        return {
            "Apple Inc": "Apple designs consumer electronics.",
            "Microsoft Corp": "Microsoft sells software.",
        }.get(name, "")


def _fake_llm(messages: list[dict[str, str]]) -> str:
    user = messages[-1]["content"]
    if "aggregate" in user:
        return json.dumps(
            [
                {
                    "statement": "Technology sector faces margin pressure.",
                    "fact_type": "margin",
                    "confidence": 0.7,
                }
            ]
        )
    if "Apple raises Q3 guidance" in user:
        return json.dumps(
            [
                {
                    "statement": "Apple guided Q3 revenue above consensus.",
                    "fact_type": "guidance",
                    "confidence": 0.8,
                }
            ]
        )
    return "REDUNDANT"


def _fake_profile_builder(name: str) -> dict:
    return {
        "description": f"{name} designs things.",
        "products_services": ["category-a", "category-b"],
        "website_url": None,
        "sources": {"wikipedia": None, "crawled": []},
        "generated_by": "deepseek-chat",
    }


def _seed_article(conn, article_id: str = "art-0") -> None:
    conn.execute(
        """
        INSERT INTO articles (id, url_hash, content_hash, url, source, ticker,
                              published_at, fetched_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processed')
        """,
        [
            article_id,
            "h-0",
            "c-0",
            "https://example.com/0",
            "reuters",
            "AAPL",
            "2026-01-01 09:00:00+00",
            "2026-01-01 10:00:00+00",
        ],
    )


def test_run_daily_ingest_end_to_end(tmp_path) -> None:
    conn = init_db(":memory:")
    try:
        description_fetcher = _CountingDescriptionFetcher()

        summary = run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=description_fetcher,
            profile_builder=_fake_profile_builder,
            llm=_fake_llm,
            raw_news_dir=tmp_path / "news",
        )

        assert summary["articles_fetched"] == 3
        assert summary["articles_skipped"] == 1
        assert summary["articles_redundant"] == 1
        assert summary["facts_written"] == 1
        assert summary["aggregate_written"] == 2
        assert summary["profiles_built"] == 2
        assert summary["profiles_failed"] == 0

        facts = get_current_facts("AAPL", DATE, conn)
        assert len(facts) == 1
        assert "consensus" in facts[0]["statement"]

        sector_facts = get_current_aggregate_facts("sector", "Technology", DATE, conn)
        assert len(sector_facts) == 1
        assert sector_facts[0]["evidence"] == [facts[0]["id"]]
        geo_facts = get_current_aggregate_facts("geography", "US", DATE, conn)
        assert len(geo_facts) == 1

        run_row = conn.execute(
            """
            SELECT articles_fetched, articles_skipped, articles_redundant,
                   facts_written
            FROM agent_runs
            """
        ).fetchone()
        assert run_row == (3, 1, 1, 1)

        raw_path = tmp_path / "news" / f"{DATE}.ndjson"
        assert raw_path.exists()
        assert len(raw_path.read_text().strip().splitlines()) == 3

        companies = conn.execute(
            "SELECT ticker, description FROM companies ORDER BY ticker"
        ).fetchall()
        assert [(c[0], c[1]) for c in companies] == [
            ("AAPL", "Apple designs consumer electronics."),
            ("MSFT", "Microsoft sells software."),
        ]
        assert description_fetcher.calls == 2
        profile_count = conn.execute("SELECT COUNT(*) FROM company_profiles").fetchone()[0]
        assert profile_count == 2
    finally:
        conn.close()


def test_description_is_cached_across_runs(tmp_path) -> None:
    conn = init_db(":memory:")
    try:
        description_fetcher = _CountingDescriptionFetcher()

        run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=description_fetcher,
            profile_builder=_fake_profile_builder,
            llm=_fake_llm,
            raw_news_dir=tmp_path / "news",
        )
        run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=description_fetcher,
            profile_builder=_fake_profile_builder,
            llm=_fake_llm,
            raw_news_dir=tmp_path / "news",
        )

        assert description_fetcher.calls == 2
        counts = conn.execute("SELECT SUM(articles_skipped) FROM agent_runs").fetchone()[0]
        assert counts == 4  # 1 in-run duplicate + 3 already-seen on the rerun
        profiles_built = conn.execute("SELECT SUM(profiles_built) FROM agent_runs").fetchone()[0]
        assert profiles_built == 2  # built once, reused on the rerun
    finally:
        conn.close()


def test_supersedes_prior_fact(tmp_path) -> None:
    conn = init_db(":memory:")
    try:
        _seed_article(conn)
        old_fact = write_fact(
            conn,
            ticker="AAPL",
            statement="CEO is Tim Cook.",
            fact_type="management",
            confidence=0.95,
            valid_from="2026-01-01",
            source_article_id="art-0",
        )

        def llm(messages: list[dict[str, str]]) -> str:
            if "aggregate" in messages[-1]["content"]:
                return "REDUNDANT"
            return json.dumps(
                [
                    {
                        "statement": "CEO is Jane Doe.",
                        "fact_type": "management",
                        "confidence": 0.9,
                        "supersedes_statement": "CEO is Tim Cook.",
                    }
                ]
            )

        def news(ticker: str, name: str) -> list[Article]:
            if ticker != "AAPL":
                return []
            return [
                _article(
                    "AAPL",
                    "https://example.com/ceo",
                    "Apple names new CEO",
                    "Jane Doe takes over.",
                )
            ]

        run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=news,
            description_fetcher=lambda name: "x",
            profile_builder=_fake_profile_builder,
            llm=llm,
            raw_news_dir=tmp_path / "news",
        )

        facts = get_current_facts("AAPL", DATE, conn)
        assert [f["statement"] for f in facts] == ["CEO is Jane Doe."]
        valid_until = conn.execute(
            "SELECT valid_until FROM ticker_facts WHERE id = ?", [old_fact]
        ).fetchone()[0]
        assert valid_until.isoformat() == DATE
    finally:
        conn.close()


def test_run_requires_llm() -> None:
    conn = init_db(":memory:")
    try:
        with pytest.raises(ValueError, match="llm"):
            run_daily_ingest(
                DATE,
                conn=conn,
                universe=UNIVERSE,
                news_fetcher=_fake_news_fetcher,
                description_fetcher=_CountingDescriptionFetcher(),
                profile_builder=_fake_profile_builder,
                llm=None,
            )
    finally:
        conn.close()


def test_backfill_creates_new_profile_version(tmp_path) -> None:
    conn = init_db(":memory:")
    try:
        run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=_CountingDescriptionFetcher(),
            profile_builder=_fake_profile_builder,
            llm=_fake_llm,
            raw_news_dir=tmp_path / "news",
        )
        summary = run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=_CountingDescriptionFetcher(),
            profile_builder=_fake_profile_builder,
            backfill=True,
            llm=_fake_llm,
            raw_news_dir=tmp_path / "news",
        )

        assert summary["profiles_built"] == 2
        rows = conn.execute(
            """
            SELECT valid_until FROM company_profiles
            WHERE ticker = 'AAPL' ORDER BY created_at
            """
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0].isoformat() == DATE
        assert rows[1][0] is None
    finally:
        conn.close()


def test_extraction_prompt_includes_bounded_categories(tmp_path) -> None:
    captured: list[str] = []

    def llm(messages: list[dict[str, str]]) -> str:
        captured.append(messages[-1]["content"])
        return _fake_llm(messages)

    conn = init_db(":memory:")
    try:
        run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=_CountingDescriptionFetcher(),
            profile_builder=_fake_profile_builder,
            llm=llm,
            raw_news_dir=tmp_path / "news",
        )
    finally:
        conn.close()

    assert captured
    assert "Products/services: category-a, category-b" in captured[0]


def test_profile_failure_does_not_stop_run(tmp_path) -> None:
    def broken_builder(name: str) -> dict:
        raise RuntimeError("profile service down")

    conn = init_db(":memory:")
    try:
        summary = run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=_CountingDescriptionFetcher(),
            profile_builder=broken_builder,
            llm=_fake_llm,
            raw_news_dir=tmp_path / "news",
        )

        assert summary["profiles_failed"] == 2
        assert summary["facts_written"] == 1  # news still processed
    finally:
        conn.close()
