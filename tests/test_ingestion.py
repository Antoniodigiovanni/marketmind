"""End-to-end tests for the daily company-level news → facts pipeline."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest

from agent.ingestion import IngestAborted, RawNewsArchive, run_daily_ingest
from data.db.aggregate_facts import get_current_aggregate_facts
from data.db.companies import get_company
from data.db.company_profiles import get_current_profile
from data.db.facts import get_current_facts
from data.db.identifiers import get_or_create_identifier, resolve_identifier
from data.db.runs import list_recent_runs
from data.sources.rss import Article

pytestmark = pytest.mark.asyncio

DATE = "2026-08-04"
DATE_D = dt.date(2026, 8, 4)

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


async def _company_id(pg_conn, ticker: str, name: str, country: str, sector: str) -> object:
    return await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value=ticker,
        valid_from=dt.date(2020, 1, 1),
        source="test",
        name=name,
        country=country,
        sector=sector,
    )


async def test_run_daily_ingest_writes_facts_and_profiles(pg_conn, tmp_path) -> None:
    summary = await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=_fake_news_fetcher,
        description_fetcher=_CountingDescriptionFetcher(),
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        raw_news_dir=tmp_path,
    )

    assert summary["articles_fetched"] == 3
    assert summary["articles_skipped"] == 1  # AAPL's duplicate URL
    assert summary["facts_written"] == 1  # only AAPL's article yields a fact
    assert summary["articles_redundant"] == 1  # MSFT's is REDUNDANT
    assert summary["profiles_built"] == 2
    # One AAPL fact lands in both the sector ("Technology") and the
    # geography ("US") aggregation dimension -- one aggregate fact each.
    assert summary["aggregate_written"] == 2

    company_id = await _company_id(pg_conn, "AAPL", "Apple Inc", "US", "Technology")
    facts = await get_current_facts(pg_conn, company_id=company_id, as_of=DATE_D)
    assert len(facts) == 1
    assert "consensus" in facts[0]["statement"]

    company = await get_company(pg_conn, company_id=company_id)
    assert company["description"] == "Apple designs consumer electronics."

    aggregates = await get_current_aggregate_facts(
        pg_conn, dimension="sector", dimension_key="Technology", as_of=DATE_D
    )
    assert len(aggregates) == 1
    assert aggregates[0]["evidence"]


async def test_run_daily_ingest_creates_companies_on_demand(pg_conn, tmp_path) -> None:
    """No pre-seeding: the pipeline must create AAPL/MSFT itself, matching
    the old DuckDB upsert_company behavior."""
    summary = await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=lambda ticker, name: [],
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        raw_news_dir=tmp_path,
    )
    assert summary["profiles_built"] == 2


async def test_run_daily_ingest_records_a_succeeded_run(pg_conn, tmp_path) -> None:
    await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=_fake_news_fetcher,
        description_fetcher=_CountingDescriptionFetcher(),
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        raw_news_dir=tmp_path,
    )

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["run_date"] == DATE_D
    assert runs[0]["counters"]["facts_written"] == 1


async def test_run_daily_ingest_dedups_an_article_across_two_companies(pg_conn, tmp_path) -> None:
    """The same story surfacing in two companies' feeds is processed once
    per company (article_companies dedup), not dropped for the second."""

    def news_fetcher(ticker: str, name: str) -> list[Article]:
        return [_article(ticker, "https://example.com/shared", "Macro story", "Affects both.")]

    summary = await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=news_fetcher,
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=lambda messages: "REDUNDANT",
        raw_news_dir=tmp_path,
    )
    assert summary["articles_fetched"] == 2
    assert summary["articles_skipped"] == 0
    assert summary["articles_redundant"] == 2


async def test_run_daily_ingest_profiles_only_skips_news(pg_conn, tmp_path) -> None:
    calls = {"news": 0}

    def news_fetcher(ticker: str, name: str) -> list[Article]:
        calls["news"] += 1
        return []

    summary = await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=news_fetcher,
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        profiles_only=True,
        raw_news_dir=tmp_path,
    )
    assert calls["news"] == 0
    assert summary["articles_fetched"] == 0
    assert summary["profiles_built"] == 2


async def test_run_daily_ingest_backfill_supersedes_existing_profile(pg_conn, tmp_path) -> None:
    await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=lambda ticker, name: [],
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        profiles_only=True,
        raw_news_dir=tmp_path,
    )
    summary = await run_daily_ingest(
        pg_conn,
        "2026-08-05",
        universe=UNIVERSE,
        news_fetcher=lambda ticker, name: [],
        description_fetcher=lambda name: "",
        profile_builder=lambda name: {
            "description": f"{name} v2.",
            "products_services": ["new-category"],
            "website_url": None,
            "sources": {},
            "generated_by": "deepseek-chat",
        },
        llm=_fake_llm,
        backfill=True,
        profiles_only=True,
        raw_news_dir=tmp_path,
    )
    assert summary["profiles_built"] == 2

    company_id = await _company_id(pg_conn, "AAPL", "Apple Inc", "US", "Technology")
    old = await get_current_profile(pg_conn, company_id=company_id, as_of=DATE_D)
    new = await get_current_profile(pg_conn, company_id=company_id, as_of=dt.date(2026, 8, 5))
    assert old["description"] == "Apple Inc designs things."  # pre-backfill value, untouched
    assert old["valid_until"] == dt.date(2026, 8, 5)  # closed out by the backfill
    assert new["description"] == "Apple Inc v2."


async def test_run_daily_ingest_resolves_ticker_as_of_not_current_holder(pg_conn, tmp_path) -> None:
    """A ticker held by a different company on the run date must resolve to
    that historical holder, not whichever company holds it today -- the
    exact bug class the identifiers.py as-of fix targets."""
    old_company = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="XYZ",
        valid_from=dt.date(2010, 1, 1),
        source="test",
        name="Old Co",
        country="US",
        sector="Energy",
    )
    await pg_conn.execute(
        "UPDATE company_identifiers SET valid_until = %s "
        "WHERE company_id = %s AND id_type = 'ticker' AND id_value = 'XYZ'",
        (dt.date(2018, 1, 1), old_company),
    )
    await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="XYZ",
        valid_from=dt.date(2022, 1, 1),
        source="test",
        name="New Co",
        country="US",
        sector="Technology",
    )

    universe = pl.DataFrame(
        {
            "ticker": ["XYZ"],
            "name": ["Old Co"],
            "country": ["US"],
            "sector": ["Energy"],
            "added_date": [dt.date(2010, 1, 1)],
            "removed_date": [dt.date(2018, 1, 1)],
            "universe": ["sp500"],
        }
    )
    await run_daily_ingest(
        pg_conn,
        "2015-06-01",
        universe=universe,
        news_fetcher=lambda ticker, name: [],
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        profiles_only=True,
        raw_news_dir=tmp_path,
    )

    old_profile = await get_current_profile(
        pg_conn, company_id=old_company, as_of=dt.date(2015, 6, 1)
    )
    assert old_profile is not None
    assert old_profile["description"] == "Old Co designs things."


async def test_run_daily_ingest_backfill_does_not_misattribute_to_current_holder(
    pg_conn, tmp_path
) -> None:
    """Backfilling a date before XYZ's identifier existed must not attach
    facts to whichever company currently holds XYZ, nor overwrite that
    company's live attributes with backfilled data -- the misattribution
    bug _ensure_company's create_identifier fallback exists to avoid."""
    current_holder = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="XYZ",
        valid_from=dt.date(2022, 1, 1),
        source="universe-csv",
        name="New Co",
        country="US",
        sector="Technology",
    )

    universe = pl.DataFrame(
        {
            "ticker": ["XYZ"],
            "name": ["Old Co"],
            "country": ["US"],
            "sector": ["Energy"],
            "added_date": [dt.date(2010, 1, 1)],
            "removed_date": [None],
            "universe": ["sp500"],
        }
    )
    await run_daily_ingest(
        pg_conn,
        "2015-06-01",
        universe=universe,
        news_fetcher=lambda ticker, name: [],
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        profiles_only=True,
        raw_news_dir=tmp_path,
    )

    current_row = await get_company(pg_conn, company_id=current_holder)
    assert current_row["name"] == "New Co"  # untouched by the backfill run
    assert current_row["sector"] == "Technology"

    backfilled_id = await resolve_identifier(
        pg_conn, id_type="ticker", id_value="XYZ", as_of=dt.date(2015, 6, 1)
    )
    assert backfilled_id is not None
    assert backfilled_id != current_holder


async def test_ensure_company_does_not_refetch_a_cached_empty_description(
    pg_conn, tmp_path
) -> None:
    """A company whose description fetch previously failed (cached as '')
    must not be refetched on every subsequent run -- only a company that
    has never been attempted (no row yet) should trigger a fetch."""
    fetcher = _CountingDescriptionFetcher()
    await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=lambda ticker, name: [],
        description_fetcher=lambda name: "",  # simulates a failed/empty fetch
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        profiles_only=True,
        raw_news_dir=tmp_path,
    )

    await run_daily_ingest(
        pg_conn,
        "2026-08-05",
        universe=UNIVERSE,
        news_fetcher=lambda ticker, name: [],
        description_fetcher=fetcher,
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        profiles_only=True,
        raw_news_dir=tmp_path,
    )
    assert fetcher.calls == 0


async def test_run_daily_ingest_isolates_a_malformed_fact_from_the_rest_of_the_run(
    pg_conn, tmp_path
) -> None:
    """A fact list the model returns with an out-of-range confidence must
    fail only that article, not escape to abort the whole run."""

    def bad_confidence_llm(messages: list[dict[str, str]]) -> str:
        user = messages[-1]["content"]
        if "aggregate" in user:
            return "REDUNDANT"
        if "Apple raises Q3 guidance" in user:
            return json.dumps(
                [
                    {
                        "statement": "Apple guided Q3 revenue above consensus.",
                        "fact_type": "guidance",
                        "confidence": 1.5,  # out of [0, 1] -- create_fact rejects this
                    }
                ]
            )
        return "REDUNDANT"

    summary = await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=_fake_news_fetcher,
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=bad_confidence_llm,
        raw_news_dir=tmp_path,
    )

    assert summary["facts_written"] == 0
    assert summary["articles_errored"] == 1

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert runs[0]["status"] == "succeeded"  # the run itself completes


async def test_run_daily_ingest_aborts_after_consecutive_failures(
    pg_conn, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("agent.ingestion.MAX_CONSECUTIVE_FAILURES", 1)

    def exploding_llm(messages: list[dict[str, str]]) -> str:
        raise RuntimeError("provider is down")

    with pytest.raises(IngestAborted):
        await run_daily_ingest(
            pg_conn,
            DATE,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=lambda name: "",
            profile_builder=_fake_profile_builder,
            llm=exploding_llm,
            raw_news_dir=tmp_path,
        )

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert runs[0]["status"] == "aborted"
    assert "consecutive" in runs[0]["error"]


async def test_run_daily_ingest_records_failed_status_on_unhandled_error(pg_conn, tmp_path) -> None:
    # A profile builder returning a dict missing "description" crashes on
    # `built["description"]`, which is read outside the try/except that
    # wraps only the call to profile_builder itself -- an unhandled error
    # distinct from IngestAborted, exercising the bare `except Exception`
    # branch in run_daily_ingest.
    def broken_profile_builder(name: str) -> dict:
        return {}

    with pytest.raises(KeyError):
        await run_daily_ingest(
            pg_conn,
            DATE,
            universe=UNIVERSE,
            news_fetcher=lambda ticker, name: [],
            description_fetcher=lambda name: "",
            profile_builder=broken_profile_builder,
            llm=_fake_llm,
            profiles_only=True,
            raw_news_dir=tmp_path,
        )

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"]


async def test_raw_news_archive_dedups_and_resumes(tmp_path) -> None:
    path = tmp_path / "2026-08-04.ndjson"
    archive = RawNewsArchive(path)
    written = archive.append([_article("AAPL", "https://example.com/a", "T1", "S1")])
    assert written == 1

    reopened = RawNewsArchive(path)
    written_again = reopened.append([_article("AAPL", "https://example.com/a", "T1", "S1")])
    assert written_again == 0
