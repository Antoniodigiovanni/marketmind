"""End-to-end tests for the daily company-level news → facts pipeline."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest

from agent.ingestion import IngestAborted, RawNewsArchive, main, run_daily_ingest
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
        # Three articles fetched, but two are the same story: the archive
        # stores distinct records, matching articles_skipped == 1 above.
        assert len(raw_path.read_text().strip().splitlines()) == 2

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


def test_profiles_only_skips_news_processing(tmp_path) -> None:
    fetched: list[str] = []

    def counting_news(ticker: str, name: str) -> list[Article]:
        fetched.append(ticker)
        return _fake_news_fetcher(ticker, name)

    conn = init_db(":memory:")
    try:
        summary = run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=counting_news,
            description_fetcher=_CountingDescriptionFetcher(),
            profile_builder=_fake_profile_builder,
            backfill=True,
            profiles_only=True,
            llm=_fake_llm,
            raw_news_dir=tmp_path / "news",
        )

        assert summary["profiles_built"] == 2
        assert summary["articles_fetched"] == 0
        assert summary["facts_written"] == 0
        assert fetched == []
        assert not (tmp_path / "news" / f"{DATE}.ndjson").exists()
    finally:
        conn.close()


def _read_ndjson(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_archive_appends_across_calls(tmp_path) -> None:
    archive = RawNewsArchive(tmp_path / f"{DATE}.ndjson")

    assert archive.append([_article("AAPL", "http://a", "A", "sa")]) == 1
    assert archive.append([_article("MSFT", "http://b", "B", "sb")]) == 1

    records = _read_ndjson(archive.path)
    assert [r["ticker"] for r in records] == ["AAPL", "MSFT"]


def test_archive_is_idempotent_on_reruns(tmp_path) -> None:
    path = tmp_path / f"{DATE}.ndjson"
    first = RawNewsArchive(path)
    first.append([_article("AAPL", "http://a", "A", "sa")])

    # Simulate a fresh process after a crash.
    second = RawNewsArchive(path)
    written = second.append(
        [_article("AAPL", "http://a", "A", "sa"), _article("MSFT", "http://b", "B", "sb")]
    )

    assert written == 1
    assert len(_read_ndjson(path)) == 2


def test_archive_tolerates_a_torn_final_line(tmp_path) -> None:
    path = tmp_path / f"{DATE}.ndjson"
    path.write_text('{"ticker": "AAPL", "url_hash": "h1"}\n{"ticker": "MS', encoding="utf-8")

    archive = RawNewsArchive(path)
    assert archive.append([_article("MSFT", "http://b", "B", "sb")]) == 1


def test_ingest_writes_news_before_it_finishes(tmp_path) -> None:
    """The archive is populated per company, not deferred to the end."""
    conn = init_db(":memory:")
    seen: list[int] = []
    news_dir = tmp_path / "news"

    def _counting_llm(messages: list[dict[str, str]]) -> str:
        path = news_dir / f"{DATE}.ndjson"
        seen.append(len(_read_ndjson(path)) if path.exists() else 0)
        return "REDUNDANT"

    run_daily_ingest(
        DATE,
        conn=conn,
        universe=UNIVERSE,
        news_fetcher=_fake_news_fetcher,
        description_fetcher=lambda name: "desc",
        profile_builder=lambda name: None,
        llm=_counting_llm,
        raw_news_dir=news_dir,
    )

    assert seen, "no extraction calls were made"
    assert seen[0] > 0, "archive was still empty during the first extraction"


def _always_failing_llm(messages: list[dict[str, str]]) -> str:
    return "not json at all"


def test_run_aborts_after_consecutive_failures(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("agent.ingestion.MAX_CONSECUTIVE_FAILURES", 2)
    conn = init_db(":memory:")

    with pytest.raises(IngestAborted, match="2 consecutive"):
        run_daily_ingest(
            DATE,
            conn=conn,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=lambda name: "desc",
            profile_builder=lambda name: None,
            llm=_always_failing_llm,
            raw_news_dir=tmp_path / "news",
        )


def test_a_success_resets_the_failure_counter(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("agent.ingestion.MAX_CONSECUTIVE_FAILURES", 2)
    conn = init_db(":memory:")
    calls = {"n": 0}

    def _alternating_llm(messages: list[dict[str, str]]) -> str:
        calls["n"] += 1
        return "not json" if calls["n"] % 2 else "REDUNDANT"

    summary = run_daily_ingest(
        DATE,
        conn=conn,
        universe=UNIVERSE,
        news_fetcher=_fake_news_fetcher,
        description_fetcher=lambda name: "desc",
        profile_builder=lambda name: None,
        llm=_alternating_llm,
        raw_news_dir=tmp_path / "news",
    )

    assert summary["articles_errored"] > 0
    assert summary["articles_redundant"] > 0


class _StubChatClient:
    """Stands in for ChatClient so main() needs no API key."""

    def complete(self, messages: list[dict[str, str]]) -> str:
        return "REDUNDANT"


def test_main_exits_non_zero_when_aborted(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["ingestion", DATE])
    monkeypatch.setattr("agent.ingestion.ChatClient", _StubChatClient)
    monkeypatch.setattr("agent.ingestion.ChromaVectorStore", lambda **kwargs: None)

    def _raise(*args, **kwargs):
        raise IngestAborted("3 consecutive extraction failures")

    monkeypatch.setattr("agent.ingestion.run_daily_ingest", _raise)

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
