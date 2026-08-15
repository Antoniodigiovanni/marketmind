# Ingestion Pipeline Postgres Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut `agent/ingestion.py`'s daily news → facts pipeline over from
DuckDB/ChromaDB to PostgreSQL, delete `agent/knowledge.py` and
`agent/embeddings.py`, and wire `server/mcp.py`'s `run_ingest`/
`get_ingest_status` tools onto the async pipeline and `data/db/runs.py`.

**Architecture:** `agent/ingestion.py::run_daily_ingest` becomes
`async def`, taking `conn: psycopg.AsyncConnection` and calling the five
already-shipped `data/db/*.py` repo modules (`facts`, `company_profiles`,
`aggregate_facts`, `companies`, `identifiers`) plus `articles` and `runs`,
instead of `agent/knowledge.py`. `run_daily_ingest_with_pool` bridges it
into the sync CLI/MCP-thread callers, mirroring
`data/filings/fetch.py::run_daily_fetch_with_pool`.

**Tech Stack:** Python 3.11+, psycopg3 (async), pytest + pytest-asyncio, the
project's `pg_conn` fixture against a real local Postgres
(`docker compose up -d`, port 5433).

## Global Constraints

- `uv run` for every command — never bare `python`/`pip`.
- Type annotations on all signatures; `from __future__ import annotations`.
- Docstrings: one-line summary + Args/Returns/Raises on public functions.
- No `print()` in library code — `loguru.logger`.
- Max line length 100.
- Never query facts/profiles/aggregates without an `as_of` — point-in-time
  is mandatory everywhere.
- Ticker → company resolution must use
  `data/db/identifiers.py::resolve_identifier(as_of=...)` as the primary
  path, never `get_current_company_id` alone.
- No AI/model/vendor names or attribution trailers in branch names, commits,
  or PR text.
- Every Postgres-touching test uses the `pg_conn` fixture
  (`tests/conftest.py`) against a real Postgres instance, one transaction
  per test, rolled back at teardown.
- Full design: `docs/superpowers/specs/2026-08-14-postgres-ingestion-cutover-design.md`.

---

## Task 1: Remove ChromaDB / `agent/embeddings.py`

**Files:**
- Delete: `agent/embeddings.py`
- Delete: `tests/test_embeddings.py`
- Modify: `pyproject.toml` (drop the `chromadb` dependency line)

**Interfaces:** None — this module has no remaining callers once Task 2
lands, but it's safe to delete now since nothing outside
`agent/ingestion.py`/`server/mcp.py::_build_vector_store` imports it, and
both of those are rewritten/deleted in later tasks anyway. Deleting it
first keeps Task 2's diff focused on the pipeline logic.

- [ ] **Step 1: Delete the files**

```bash
git rm agent/embeddings.py tests/test_embeddings.py
```

- [ ] **Step 2: Drop the dependency**

In `pyproject.toml`, remove this line from `dependencies`:

```toml
    "chromadb>=0.5",
```

So the `# Storage` block reads:

```toml
    # Storage
    "duckdb>=0.10",
```

- [ ] **Step 3: Verify nothing else references chromadb**

```bash
grep -rn "chromadb\|ChromaVectorStore\|HashEmbeddingFunction" --include="*.py" agent server data tests
```

Expected: no output except matches inside `agent/ingestion.py` and
`server/mcp.py` (still present — cleaned up in Tasks 2 and 4) and
`tests/test_mcp_jobs.py` (cleaned up in Task 4).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "Remove ChromaDB vector store, superseded by pgvector"
```

---

## Task 2: `agent/ingestion.py` async rewrite

**Files:**
- Modify: `agent/ingestion.py` (full rewrite)
- Modify: `tests/test_ingestion.py` (full rewrite)

**Interfaces:**
- Consumes: `data/db/facts.py::create_fact(conn, *, company_id, statement, fact_type, confidence, valid_from, source_article_id, embed, model, supersedes_id=None) -> uuid.UUID`,
  `get_current_facts(conn, *, company_id, as_of) -> list[dict]`,
  `search_similar_facts(conn, *, company_id, query, as_of, embed, top_k=5) -> list[dict]`;
  `data/db/company_profiles.py::create_profile(conn, *, company_id, description, products_services, valid_from, generated_by, website_url=None, sources=None, supersedes_id=None) -> uuid.UUID`,
  `get_current_profile(conn, *, company_id, as_of) -> dict | None`;
  `data/db/aggregate_facts.py::create_aggregate_fact(conn, *, dimension, dimension_key, statement, fact_type, confidence, valid_from, evidence, embed, model, supersedes_id=None) -> uuid.UUID`;
  `data/db/companies.py::get_company(conn, *, company_id) -> dict | None`,
  `update_company_description(conn, *, company_id, description, description_source) -> None`;
  `data/db/identifiers.py::resolve_identifier(conn, *, id_type, id_value, as_of) -> uuid.UUID | None`,
  `get_or_create_identifier(conn, *, id_type, id_value, valid_from, source, name, country, sector) -> uuid.UUID`;
  `data/db/articles.py::register_article(conn, *, company_id, url_hash, content_hash, url, source, title, published_at, pre_score) -> uuid.UUID | None`,
  `set_article_status(conn, *, article_id, company_id, status, error=None) -> None`;
  `data/db/runs.py::start_run(conn, *, run_date, job_type) -> uuid.UUID`,
  `finish_run(conn, *, run_id, status, counters, error=None) -> None`;
  `data/db/pool.py::open_pool(dsn=None, ...) -> AsyncConnectionPool`, `close_pool(pool) -> None`;
  `agent/embedding.py::hash_embed(text: str) -> Awaitable[list[float]]`.
- Produces: `run_daily_ingest(conn, date, *, universe=None, news_fetcher=None, description_fetcher=None, profile_builder=None, backfill=False, profiles_only=False, llm=None, raw_news_dir=None, progress=None) -> dict[str, int]` (now `async def`, first positional arg is the connection);
  `run_daily_ingest_with_pool(date, **kwargs) -> dict[str, int]` (`async def`);
  `IngestAborted`, `RawNewsArchive`, `pre_score`, `_default_profile_builder`, `MAX_CONSECUTIVE_FAILURES` — all unchanged in name/shape, consumed by `server/mcp.py` in Task 4.

### Step 1: Write the new test file

- [ ] Replace `tests/test_ingestion.py` with:

```python
"""End-to-end tests for the daily company-level news → facts pipeline."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest

from agent.ingestion import IngestAborted, RawNewsArchive, run_daily_ingest
from data.db.aggregate_facts import get_current_aggregate_facts
from data.db.companies import get_company
from data.db.facts import get_current_facts
from data.db.identifiers import get_or_create_identifier
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


async def _seed_universe_companies(pg_conn) -> None:
    """Pre-register AAPL/MSFT so tests don't depend on creation-on-demand
    for scenarios that aren't specifically testing that path."""
    for ticker, name, country, sector in (
        ("AAPL", "Apple Inc", "US", "Technology"),
        ("MSFT", "Microsoft Corp", "US", "Technology"),
    ):
        await get_or_create_identifier(
            pg_conn,
            id_type="ticker",
            id_value=ticker,
            valid_from=dt.date(2020, 1, 1),
            source="test",
            name=name,
            country=country,
            sector=sector,
        )


async def test_run_daily_ingest_writes_facts_and_profiles(pg_conn) -> None:
    summary = await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=_fake_news_fetcher,
        description_fetcher=_CountingDescriptionFetcher(),
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
    )

    assert summary["articles_fetched"] == 3
    assert summary["articles_skipped"] == 1  # AAPL's duplicate URL
    assert summary["facts_written"] == 1  # only AAPL's article yields a fact
    assert summary["articles_redundant"] == 1  # MSFT's is REDUNDANT
    assert summary["profiles_built"] == 2
    assert summary["aggregate_written"] == 1

    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2020, 1, 1),
        source="test",
        name="Apple Inc",
        country="US",
        sector="Technology",
    )
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


async def test_run_daily_ingest_creates_companies_on_demand(pg_conn) -> None:
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
    )
    assert summary["profiles_built"] == 2


async def test_run_daily_ingest_records_a_succeeded_run(pg_conn) -> None:
    await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=_fake_news_fetcher,
        description_fetcher=_CountingDescriptionFetcher(),
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
    )

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["run_date"] == DATE_D
    assert runs[0]["counters"]["facts_written"] == 1


async def test_run_daily_ingest_dedups_an_article_across_two_companies(pg_conn) -> None:
    """The same story surfacing in two companies' feeds is processed once
    per company (article_companies dedup), not dropped for the second."""
    shared = _article(
        "AAPL", "https://example.com/shared", "Macro story", "Affects both."
    )

    def news_fetcher(ticker: str, name: str) -> list[Article]:
        return [Article(**{**shared.__dict__, "ticker": ticker})]

    summary = await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=news_fetcher,
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=lambda messages: "REDUNDANT",
    )
    assert summary["articles_fetched"] == 2
    assert summary["articles_skipped"] == 0
    assert summary["articles_redundant"] == 2


async def test_run_daily_ingest_profiles_only_skips_news(pg_conn) -> None:
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
    )
    assert calls["news"] == 0
    assert summary["articles_fetched"] == 0
    assert summary["profiles_built"] == 2


async def test_run_daily_ingest_backfill_supersedes_existing_profile(pg_conn) -> None:
    await run_daily_ingest(
        pg_conn,
        DATE,
        universe=UNIVERSE,
        news_fetcher=lambda ticker, name: [],
        description_fetcher=lambda name: "",
        profile_builder=_fake_profile_builder,
        llm=_fake_llm,
        profiles_only=True,
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
    )
    assert summary["profiles_built"] == 2

    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2020, 1, 1),
        source="test",
        name="Apple Inc",
        country="US",
        sector="Technology",
    )
    from data.db.company_profiles import get_current_profile

    old = await get_current_profile(pg_conn, company_id=company_id, as_of=DATE_D)
    new = await get_current_profile(pg_conn, company_id=company_id, as_of=dt.date(2026, 8, 5))
    assert old["description"] == "Apple Inc v2."  # superseded by the backfill
    assert old["valid_until"] == dt.date(2026, 8, 5)
    assert new["description"] == "Apple Inc v2."


async def test_run_daily_ingest_resolves_ticker_as_of_not_current_holder(pg_conn) -> None:
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
    )

    from data.db.company_profiles import get_current_profile

    old_profile = await get_current_profile(
        pg_conn, company_id=old_company, as_of=dt.date(2015, 6, 1)
    )
    assert old_profile is not None
    assert old_profile["description"] == "Old Co designs things."


async def test_run_daily_ingest_aborts_after_consecutive_failures(pg_conn, monkeypatch) -> None:
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
        )

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert runs[0]["status"] == "aborted"
    assert "consecutive" in runs[0]["error"]


async def test_run_daily_ingest_records_failed_status_on_unhandled_error(pg_conn) -> None:
    def broken_profile_builder(name: str) -> dict:
        raise RuntimeError("boom")

    def broken_news_fetcher(ticker: str, name: str) -> list[Article]:
        raise ValueError("network is unreachable")  # escapes the per-ticker try/except? No --
        # news fetch failures are caught per-ticker; use a bug in the
        # aggregation loop instead by forcing a non-dict LLM response.

    with pytest.raises(TypeError):
        await run_daily_ingest(
            pg_conn,
            DATE,
            universe=UNIVERSE,
            news_fetcher=_fake_news_fetcher,
            description_fetcher=lambda name: "",
            profile_builder=_fake_profile_builder,
            llm=lambda messages: None,  # .strip() on None raises TypeError inside parsing
        )

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"]


async def test_raw_news_archive_dedups_and_resumes(tmp_path) -> None:
    path = tmp_path / "2026-08-04.ndjson"
    archive = RawNewsArchive(path)
    written = archive.append(
        [_article("AAPL", "https://example.com/a", "T1", "S1")]
    )
    assert written == 1

    reopened = RawNewsArchive(path)
    written_again = reopened.append(
        [_article("AAPL", "https://example.com/a", "T1", "S1")]
    )
    assert written_again == 0
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
uv run pytest tests/test_ingestion.py -v
```

Expected: FAIL/ERROR — `run_daily_ingest` still takes a DuckDB connection
and is not `async`, `data.db.runs.list_recent_runs` isn't imported by
anything ingestion-related yet, etc. (Collection or immediate failures on
every test.)

### Step 3: Rewrite `agent/ingestion.py`

- [ ] Replace the full contents of `agent/ingestion.py` with:

```python
"""Daily company-level news → facts pipeline with sector/geography aggregates."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
from loguru import logger

from agent.company_profile import build_company_profile
from agent.embedding import hash_embed
from agent.llm import ChatClient, parse_json_response
from data.db.aggregate_facts import create_aggregate_fact
from data.db.articles import register_article, set_article_status
from data.db.companies import get_company, update_company_description
from data.db.company_profiles import create_profile, get_current_profile
from data.db.facts import create_fact, get_current_facts, search_similar_facts
from data.db.identifiers import get_or_create_identifier, resolve_identifier
from data.db.pool import close_pool, open_pool
from data.db.runs import finish_run, start_run
from data.sources.rss import Article, article_to_record, fetch_company_news
from data.sources.wikipedia import fetch_company_description
from data.universe.loader import get_universe

SOURCE_CREDIBILITY: dict[str, float] = {
    "google-news": 0.55,
    "yahoo-finance": 0.6,
}
RECENCY_LAMBDA = 0.05

# Consecutive extraction failures tolerated before the run gives up. A provider
# outage otherwise burns the whole universe to 'error' over hours of dead calls.
MAX_CONSECUTIVE_FAILURES = int(os.getenv("INGEST_MAX_CONSECUTIVE_FAILURES", "20"))

RAW_NEWS_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "news"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

NewsFetcher = Callable[[str, str], list[Article]]
DescriptionFetcher = Callable[[str], str]
LLMCallable = Callable[[list[dict[str, str]]], str]
ProfileBuilder = Callable[[str], dict[str, Any] | None]
# (tickers_done, tickers_total, running summary counts)
ProgressCallback = Callable[[int, int, dict[str, int]], None]


class IngestAborted(RuntimeError):
    """Raised when consecutive extraction failures trip the circuit breaker."""


def _load_prompt(name: str) -> str:
    """Read a prompt template from agent/prompts."""
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def pre_score(source: str, published_at: str, as_of: str) -> float:
    """Compute source credibility × exponential recency decay."""
    credibility = SOURCE_CREDIBILITY.get(source, 0.5)
    published = dt.datetime.fromisoformat(published_at)
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.UTC)
    as_of_midnight = dt.datetime.combine(dt.date.fromisoformat(as_of), dt.time.min, tzinfo=dt.UTC)
    days_old = max((as_of_midnight - published).total_seconds() / 86_400.0, 0.0)
    return credibility * math.exp(-RECENCY_LAMBDA * days_old)


def parse_fact_list_response(text: str) -> list[dict[str, Any]] | None:
    """Parse a model response into a fact list, or None for REDUNDANT."""
    cleaned = text.strip()
    if cleaned.upper().startswith("REDUNDANT"):
        return None
    parsed = parse_json_response(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("model response must be a JSON list of facts")
    for item in parsed:
        if not isinstance(item, dict) or "statement" not in item:
            raise ValueError("each fact must include a statement")
    return parsed


def build_extraction_messages(
    *,
    article: Article,
    company_name: str,
    description: str,
    products_services: str,
    sector: str | None,
    country: str | None,
    current_facts: list[dict[str, object]],
    similar_facts: list[dict[str, object]],
    score: float,
    date: str,
) -> list[dict[str, str]]:
    """Render the fact-extraction prompt for one article."""
    template = _load_prompt("fact_extraction.md")
    current_block = (
        "\n".join(
            f"- [{f['fact_id']}] ({f['fact_type']}) {f['statement']}" for f in current_facts
        )
        or "(none)"
    )
    similar_block = (
        "\n".join(
            f"- [{f['fact_id']}] ({f['fact_type']}) {f['statement']}" for f in similar_facts
        )
        or "(none)"
    )
    prompt = template.format(
        article_title=article.title,
        article_summary=article.summary,
        article_url=article.url,
        article_published=article.published_at,
        date=date,
        ticker=article.ticker,
        company_name=company_name,
        sector=sector or "unknown",
        country=country or "unknown",
        company_description=description or "(no description available)",
        products_services=products_services or "(none)",
        current_facts=current_block,
        similar_facts=similar_block,
        pre_score=f"{score:.3f}",
    )
    return [
        {"role": "system", "content": "You are a precise market-research fact extractor."},
        {"role": "user", "content": prompt},
    ]


def _default_profile_builder(llm: LLMCallable) -> ProfileBuilder:
    """Return a profile builder bound to the injected chat client."""

    def builder(name: str) -> dict[str, Any] | None:
        return build_company_profile(name, llm=llm, model="deepseek-chat")

    return builder


def build_aggregate_messages(
    *,
    dimension: str,
    dimension_key: str,
    facts: list[dict[str, str]],
    date: str,
) -> list[dict[str, str]]:
    """Render the aggregate fact-extraction prompt for one group."""
    template = _load_prompt("aggregate_fact_extraction.md")
    facts_block = (
        "\n".join(f"- {f['ticker']} ({f['fact_type']}): {f['statement']}" for f in facts)
        or "(none)"
    )
    prompt = template.format(
        dimension=dimension,
        dimension_key=dimension_key,
        date=date,
        facts=facts_block,
    )
    return [
        {"role": "system", "content": "You are a precise market-research aggregator."},
        {"role": "user", "content": prompt},
    ]


async def _ensure_company(
    conn: psycopg.AsyncConnection,
    *,
    ticker: str,
    name: str,
    country: str | None,
    sector: str | None,
    as_of: dt.date,
    description_fetcher: DescriptionFetcher,
) -> tuple[uuid.UUID, str]:
    """Resolve or create the company holding `ticker` as of a date.

    Refreshes the cached description (and, via get_or_create_identifier,
    name/country/sector) only the first time a company has no description
    yet -- matching the old DuckDB pipeline's lazy upsert_company gate,
    not a refresh on every run.

    Args:
        conn: An open connection.
        ticker: Ticker symbol from the point-in-time universe row.
        name: Company display name from the universe row.
        country: ISO country code from the universe row, if known.
        sector: Sector label from the universe row, if known.
        as_of: The run date -- resolution is point-in-time safe.
        description_fetcher: Callable(name) -> description text.

    Returns:
        (company_id, description) -- description is the cached value when
        present, otherwise the freshly fetched one (possibly "").
    """
    company_id = await resolve_identifier(conn, id_type="ticker", id_value=ticker, as_of=as_of)
    if company_id is None:
        company_id = await get_or_create_identifier(
            conn,
            id_type="ticker",
            id_value=ticker,
            valid_from=as_of,
            source="ingest",
            name=name,
            country=country,
            sector=sector,
        )

    company = await get_company(conn, company_id=company_id)
    description = company["description"] if company else None
    if not description:
        try:
            description = description_fetcher(name)
        except Exception as exc:  # noqa: BLE001 - one company must not sink the run
            logger.warning(f"description fetch failed for {ticker}: {exc}")
            description = ""
        await update_company_description(
            conn,
            company_id=company_id,
            description=description,
            description_source="wikipedia" if description else None,
        )
    return company_id, description or ""


async def apply_facts(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    ticker: str,
    date: dt.date,
    article_id: uuid.UUID,
    facts: list[dict[str, Any]],
    current_facts: list[dict[str, object]],
) -> tuple[int, int, list[dict[str, object]]]:
    """Write extracted facts, retiring superseded ones.

    Args:
        conn: An open connection.
        company_id: The facts' subject.
        ticker: Ticker symbol, carried through to the aggregation step.
        date: Date the facts became valid.
        article_id: Source article for every fact written here.
        facts: Parsed model output, one dict per fact.
        current_facts: This company's facts as of `date`, used to resolve
            `supersedes_statement` references by exact statement match.

    Returns:
        (facts written, facts superseded, written fact rows for aggregation).
    """
    statements = {str(f["statement"]): f["fact_id"] for f in current_facts}
    written = 0
    superseded = 0
    rows: list[dict[str, object]] = []
    for fact in facts:
        supersede_stmt = str(fact.get("supersedes_statement") or "").strip()
        supersedes_id = statements.get(supersede_stmt)
        fact_id = await create_fact(
            conn,
            company_id=company_id,
            statement=str(fact["statement"]),
            fact_type=str(fact.get("fact_type") or "general"),
            confidence=float(fact["confidence"]),
            valid_from=date,
            source_article_id=article_id,
            embed=hash_embed,
            model="hash-ngram",
            supersedes_id=supersedes_id,
        )
        rows.append(
            {
                "ticker": ticker,
                "fact_id": fact_id,
                "fact_type": str(fact.get("fact_type") or "general"),
                "statement": str(fact["statement"]),
            }
        )
        written += 1
        if supersedes_id:
            superseded += 1
    return written, superseded, rows


class RawNewsArchive:
    """Append-only NDJSON archive of fetched articles for one date.

    Written incrementally so a run killed partway through keeps the articles it
    already fetched, and so the MCP news tools return rows during the run
    rather than only after it. Re-running the same date appends only records
    whose url_hash is not already on disk, which keeps the file immutable in
    content while remaining safe to resume.

    Single-writer: `append` does an unsynchronized read-modify-write on
    `_seen` plus a file append, so concurrent callers can race. Safe today
    because the ingest loop is single-threaded; needs an external lock before
    any concurrent use.
    """

    def __init__(self, path: Path) -> None:
        """Open (or adopt) the archive for a date and index what is already stored."""
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen = self._existing_hashes()

    def _existing_hashes(self) -> set[str]:
        """Read the url_hash of every complete record already in the file."""
        if not self.path.exists():
            return set()
        hashes: set[str] = set()
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    hashes.add(str(json.loads(line)["url_hash"]))
                except (json.JSONDecodeError, KeyError, TypeError):
                    # A torn final line from a killed run — ignore and move on.
                    continue
        return hashes

    def append(self, articles: list[Article]) -> int:
        """Append articles not already archived.

        Dedup is checked incrementally against `_seen` as each article is
        considered, not pre-filtered in one pass — so two duplicate articles
        arriving in the same call (e.g. the same story fetched twice in one
        run) are also caught, not just duplicates across separate calls.

        Args:
            articles: Fetched articles, possibly overlapping what is stored.

        Returns:
            Number of records actually written.
        """
        new: list[Article] = []
        for article in articles:
            if article.url_hash in self._seen:
                continue
            self._seen.add(article.url_hash)
            new.append(article)
        if not new:
            return 0
        with self.path.open("a", encoding="utf-8") as handle:
            for article in new:
                handle.write(json.dumps(article_to_record(article)) + "\n")
            handle.flush()
        return len(new)


async def run_daily_ingest(
    conn: psycopg.AsyncConnection,
    date: str,
    *,
    universe: pl.DataFrame | None = None,
    news_fetcher: NewsFetcher | None = None,
    description_fetcher: DescriptionFetcher | None = None,
    profile_builder: ProfileBuilder | None = None,
    backfill: bool = False,
    profiles_only: bool = False,
    llm: LLMCallable | None = None,
    raw_news_dir: Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Run the full daily news → facts pipeline.

    Steps:
    1. Load the point-in-time universe for the date.
    2. Per company: resolve or create the company, cache metadata, ensure a
       generated profile (lazy unless backfill), fetch news (RSS, keyless),
       and append it to the raw news archive for the date (deduped on
       url_hash, so a rerun is safe).
    3. Article dedup + per-company attribution via data/db/articles.py.
    4. Pre-score each article.
    5. Per new (article, company) pair: retrieve current + similar past
       facts, call the model, and write (or retire) facts.
    6. Aggregate the day's new facts by sector and country into
       aggregate_facts.
    7. Write an agent_runs audit row (started before the loop, finished
       after — so a killed or crashed run still leaves a trace).

    Args:
        conn: An open connection, held for the whole run. Each repository
            call commits its own work independently -- there is no single
            enclosing transaction, so a crash mid-run keeps whatever was
            already committed rather than rolling all of it back.
        date: Date string (YYYY-MM-DD).
        universe: Optional point-in-time universe frame (defaults to loader).
        news_fetcher: Callable(ticker, name) -> list[Article] (defaults to RSS).
        description_fetcher: Callable(name) -> str (defaults to Wikipedia).
        profile_builder: Callable(name) -> profile dict or None (defaults to
            build_company_profile bound to llm).
        backfill: When True, build a new profile version for every company
            (annual refresh); when False, only build missing profiles.
        profiles_only: When True, only ensure profiles; skip news fetch,
            fact extraction, and aggregates (used by the annual refresh job).
        llm: Callable(messages) -> text; required.
        raw_news_dir: Directory for the immutable per-date NDJSON file.
        progress: Optional callable(tickers_done, tickers_total, summary)
            invoked after each universe row, so a caller can report live
            progress before the agent_runs row is finished at the end.

    Returns:
        Summary counts keyed by articles_fetched, articles_skipped,
        articles_redundant, articles_errored, facts_written,
        facts_superseded, aggregate_written, profiles_built, profiles_failed.

    Raises:
        ValueError: On an invalid date or a missing llm callable.
        IngestAborted: After MAX_CONSECUTIVE_FAILURES consecutive extraction
            failures. The run is recorded with status='aborted' first.
    """
    date_as_date = dt.date.fromisoformat(date)
    if llm is None:
        raise ValueError("llm callable is required")
    description_fetcher = description_fetcher or fetch_company_description

    universe = universe if universe is not None else get_universe(date)
    raw_news_dir = raw_news_dir or RAW_NEWS_DIR
    archive = RawNewsArchive(raw_news_dir / f"{date}.ndjson")

    summary = {
        "articles_fetched": 0,
        "articles_skipped": 0,
        "articles_redundant": 0,
        "articles_errored": 0,
        "facts_written": 0,
        "facts_superseded": 0,
        "aggregate_written": 0,
        "profiles_built": 0,
        "profiles_failed": 0,
    }
    todays_facts: list[dict[str, object]] = []
    consecutive_failures = 0
    tickers_total = universe.height
    tickers_done = 0

    run_id = await start_run(conn, run_date=date_as_date, job_type="news")

    try:
        for row in universe.iter_rows(named=True):
            ticker = str(row["ticker"])
            name = str(row["name"])
            country_str = str(row["country"]) if row.get("country") else None
            sector_str = str(row["sector"]) if row.get("sector") else None

            company_id, description = await _ensure_company(
                conn,
                ticker=ticker,
                name=name,
                country=country_str,
                sector=sector_str,
                as_of=date_as_date,
                description_fetcher=description_fetcher,
            )

            profile = await get_current_profile(conn, company_id=company_id, as_of=date_as_date)
            if profile is None or backfill:
                try:
                    built = (profile_builder or _default_profile_builder(llm))(name)
                except Exception as exc:  # noqa: BLE001 - one company must not sink the run
                    logger.warning(f"profile build failed for {ticker}: {exc}")
                    built = None
                if built is None:
                    summary["profiles_failed"] += 1
                else:
                    await create_profile(
                        conn,
                        company_id=company_id,
                        description=str(built["description"]),
                        products_services=[
                            str(c) for c in built.get("products_services") or []
                        ],
                        valid_from=date_as_date,
                        generated_by=str(built.get("generated_by") or "deepseek-chat"),
                        website_url=built.get("website_url"),
                        sources=built.get("sources"),
                        supersedes_id=profile["profile_id"] if backfill and profile else None,
                    )
                    summary["profiles_built"] += 1
                    profile = await get_current_profile(
                        conn, company_id=company_id, as_of=date_as_date
                    )

            profile_description = profile["description"] if profile else description
            profile_categories = (
                ", ".join(profile["products_services"])
                if profile and profile["products_services"]
                else "(none)"
            )

            if not profiles_only:
                try:
                    articles = (
                        news_fetcher(ticker, name)
                        if news_fetcher is not None
                        else fetch_company_news(ticker, name)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"news fetch failed for {ticker}: {exc}")
                    articles = []
                archive.append(articles)
                summary["articles_fetched"] += len(articles)

                for article in articles:
                    score = pre_score(article.source, article.published_at, date)
                    published = dt.datetime.fromisoformat(article.published_at)
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=dt.UTC)
                    article_id = await register_article(
                        conn,
                        company_id=company_id,
                        url_hash=article.url_hash,
                        content_hash=article.content_hash,
                        url=article.url,
                        source=article.source,
                        title=article.title,
                        published_at=published,
                        pre_score=score,
                    )
                    if article_id is None:
                        summary["articles_skipped"] += 1
                        continue

                    current_facts = await get_current_facts(
                        conn, company_id=company_id, as_of=date_as_date
                    )
                    similar_facts = await search_similar_facts(
                        conn,
                        company_id=company_id,
                        query=f"{article.title} {article.summary}",
                        as_of=date_as_date,
                        embed=hash_embed,
                    )
                    messages = build_extraction_messages(
                        article=article,
                        company_name=name,
                        description=profile_description,
                        products_services=profile_categories,
                        sector=sector_str,
                        country=country_str,
                        current_facts=current_facts,
                        similar_facts=similar_facts,
                        score=score,
                        date=date,
                    )
                    try:
                        parsed = parse_fact_list_response(llm(messages))
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"extraction failed for article {article_id}: {exc}")
                        await set_article_status(
                            conn,
                            article_id=article_id,
                            company_id=company_id,
                            status="error",
                            error=str(exc),
                        )
                        summary["articles_errored"] += 1
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            raise IngestAborted(
                                f"{consecutive_failures} consecutive extraction failures; "
                                f"aborting run for {date}"
                            ) from exc
                        continue

                    consecutive_failures = 0

                    if parsed is None:
                        await set_article_status(
                            conn, article_id=article_id, company_id=company_id, status="redundant"
                        )
                        summary["articles_redundant"] += 1
                        continue

                    written, superseded, fact_rows = await apply_facts(
                        conn,
                        company_id=company_id,
                        ticker=ticker,
                        date=date_as_date,
                        article_id=article_id,
                        facts=parsed,
                        current_facts=current_facts,
                    )
                    await set_article_status(
                        conn, article_id=article_id, company_id=company_id, status="processed"
                    )
                    summary["facts_written"] += written
                    summary["facts_superseded"] += superseded
                    for fact_row in fact_rows:
                        todays_facts.append(
                            {
                                "ticker": fact_row["ticker"],
                                "fact_id": fact_row["fact_id"],
                                "fact_type": fact_row["fact_type"],
                                "statement": fact_row["statement"],
                                "sector": sector_str,
                                "country": country_str,
                            }
                        )

            tickers_done += 1
            if progress is not None:
                progress(tickers_done, tickers_total, summary)

        for dimension, key_field in (("sector", "sector"), ("geography", "country")):
            groups: dict[str, list[dict[str, object]]] = {}
            for fact in todays_facts:
                key = fact[key_field]
                if key:
                    groups.setdefault(str(key), []).append(fact)
            for dimension_key, group_facts in groups.items():
                messages = build_aggregate_messages(
                    dimension=dimension,
                    dimension_key=dimension_key,
                    facts=[
                        {
                            "ticker": str(f["ticker"]),
                            "fact_type": str(f["fact_type"]),
                            "statement": str(f["statement"]),
                        }
                        for f in group_facts
                    ],
                    date=date,
                )
                try:
                    parsed = parse_fact_list_response(llm(messages))
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        f"aggregate extraction failed for {dimension}={dimension_key}: {exc}"
                    )
                    continue
                if parsed is None:
                    continue
                evidence = [f["fact_id"] for f in group_facts]
                for item in parsed:
                    await create_aggregate_fact(
                        conn,
                        dimension=dimension,
                        dimension_key=dimension_key,
                        statement=str(item["statement"]),
                        fact_type=str(item.get("fact_type") or "general"),
                        confidence=float(item["confidence"]),
                        valid_from=date_as_date,
                        evidence=evidence,
                        embed=hash_embed,
                        model="hash-ngram",
                    )
                    summary["aggregate_written"] += 1

    except IngestAborted as exc:
        await finish_run(conn, run_id=run_id, status="aborted", counters=summary, error=str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - always record the outcome before propagating
        await finish_run(conn, run_id=run_id, status="failed", counters=summary, error=str(exc))
        raise
    else:
        await finish_run(conn, run_id=run_id, status="succeeded", counters=summary)

    return summary


async def run_daily_ingest_with_pool(date: str, **kwargs: Any) -> dict[str, int]:
    """Open a Postgres pool for the duration of one run_daily_ingest call.

    Bridges the async repository layer into this module's synchronous CLI
    entry point (main()) and, indirectly, into the thread-based
    server.jobs.start_run work callback used by the run_ingest MCP tool --
    asyncio.run() at that boundary is an acceptable bridge, matching
    data/filings/fetch.py::run_daily_fetch_with_pool.

    Args:
        date: Date string (YYYY-MM-DD) to ingest.
        **kwargs: Forwarded to run_daily_ingest.

    Returns:
        The summary dict from run_daily_ingest.
    """
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            return await run_daily_ingest(conn, date, **kwargs)
    finally:
        await close_pool(pool)


def main() -> None:
    """Run the daily ingest job from the command line."""
    import asyncio

    parser = argparse.ArgumentParser(description="Daily news → facts ingest")
    parser.add_argument("date", help="YYYY-MM-DD")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="build a new profile version for every company (annual refresh)",
    )
    parser.add_argument(
        "--profiles-only",
        action="store_true",
        help="only ensure/refresh profiles; skip news processing",
    )
    args = parser.parse_args()

    client = ChatClient()
    try:
        summary = asyncio.run(
            run_daily_ingest_with_pool(
                args.date,
                llm=client.complete,
                profile_builder=_default_profile_builder(client.complete),
                backfill=args.backfill,
                profiles_only=args.profiles_only,
            )
        )
    except IngestAborted as exc:
        logger.error(f"ingest aborted for {args.date}: {exc}")
        sys.exit(1)
    logger.info(f"ingest summary for {args.date}: {summary}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests**

```bash
docker compose up -d
uv run pytest tests/test_ingestion.py -v
```

Expected: all tests PASS. If `test_run_daily_ingest_records_failed_status_on_unhandled_error`
fails because `.strip()` on `None` doesn't raise inside your environment's
`parse_json_response`/`parse_fact_list_response` call path, adjust the fake
`llm` in that one test to whatever expression reliably raises before any
DB write in that branch (e.g. `lambda messages: 1 / 0`) — the point of the
test is only that a non-`IngestAborted` exception still produces a
`status='failed'` row, not the exact exception type.

- [ ] **Step 5: Lint and format**

```bash
uv run ruff check agent/ingestion.py tests/test_ingestion.py
uv run ruff format agent/ingestion.py tests/test_ingestion.py
```

Fix any reported issues, then re-run Step 4.

- [ ] **Step 6: Commit**

```bash
git add agent/ingestion.py tests/test_ingestion.py
git commit -m "Cut daily ingestion pipeline over to PostgreSQL"
```

---

## Task 3: Delete `agent/knowledge.py`

**Files:**
- Delete: `agent/knowledge.py`
- Delete: `tests/test_knowledge.py`

**Interfaces:** None — Task 2 already removed the only production import
(`agent/ingestion.py`). Confirm no other importer exists before deleting.

- [ ] **Step 1: Confirm no remaining importers**

```bash
grep -rln "agent\.knowledge\|from agent import knowledge" --include="*.py" agent server data tests
```

Expected: no output (only `tests/test_knowledge.py` itself would match,
and it's deleted in this task).

- [ ] **Step 2: Delete the files**

```bash
git rm agent/knowledge.py tests/test_knowledge.py
```

- [ ] **Step 3: Run the full test suite to catch any missed importer**

```bash
uv run pytest tests/ -v --ignore=tests/test_mcp_jobs.py -x
```

Expected: PASS (excluding `test_mcp_jobs.py`, which is still on the old
DuckDB/ChromaDB assumptions until Task 4).

- [ ] **Step 4: Commit**

```bash
git commit -m "Delete agent/knowledge.py, superseded by data/db/*.py repositories"
```

---

## Task 4: `server/mcp.py` — `run_ingest` / `get_ingest_status` cutover

**Files:**
- Modify: `server/mcp.py`
- Modify: `tests/test_mcp_jobs.py`

**Interfaces:**
- Consumes: `agent/ingestion.py::run_daily_ingest_with_pool(date, **kwargs) -> dict[str, int]`,
  `_default_profile_builder(llm) -> ProfileBuilder` (both from Task 2);
  `data/db/runs.py::list_recent_runs(conn, *, job_type=None, limit=5) -> list[dict]`.
- Produces: `run_ingest(date, profiles_only=False, backfill=False, tickers=None) -> str` (unchanged signature, `def`, not `async`);
  `get_ingest_status(limit=5) -> dict[str, object]` (now `async def`).

### Step 1: Update `server/mcp.py`

- [ ] In the imports block (currently lines 1–33), leave the `duckdb`
  import alone (still needed by `_connect()` for filings) and add
  `list_recent_runs` alongside the existing `data.db` imports:

```python
from data.db.runs import list_recent_runs
```

Place it alphabetically among the existing `from data.db...` import lines
(after `from data.db.pool import open_pool`, before
`from data.sources.rss import NEWS_RECORD_FIELDS`).

- [ ] Delete the `AGENT_RUN_COLUMNS` constant (currently lines 54–74):

```python
# Columns of agent_runs surfaced by get_ingest_status, in report order.
AGENT_RUN_COLUMNS = [
    "id",
    "run_date",
    "job_type",
    "started_at",
    "finished_at",
    "articles_fetched",
    "articles_skipped",
    "articles_redundant",
    "facts_written",
    "facts_superseded",
    "profiles_built",
    "profiles_failed",
    "filings_fetched",
    "filings_parsed",
    "filings_skipped",
    "insider_rows",
    "form13f_rows",
    "error",
]
```

Delete this block entirely.

- [ ] Delete `_build_vector_store` (currently around line 972-976):

```python
def _build_vector_store() -> object:
    """Return the fact-embedding store used to retrieve similar past facts."""
    from agent.embeddings import ChromaVectorStore, HashEmbeddingFunction

    return ChromaVectorStore(embedding_function=HashEmbeddingFunction())
```

Delete this function entirely.

- [ ] Replace `run_ingest`'s body (currently the block from
  `from agent.ingestion import _default_profile_builder, run_daily_ingest`
  through the `return message` at the end of the function) with:

```python
@mcp.tool()
def run_ingest(
    date: str,
    profiles_only: bool = False,
    backfill: bool = False,
    tickers: list[str] | None = None,
) -> str:
    """Start the daily news → facts ingest for a date, in the background.

    The pipeline takes hours over the full universe, so this returns as soon
    as the run is accepted. Poll get_ingest_status() for progress. Only one
    run may be in flight at a time.

    Args:
        date: Date string (YYYY-MM-DD) to ingest.
        profiles_only: Only ensure/refresh company profiles; skip news fetch,
            fact extraction, and aggregates.
        backfill: Build a new profile version for every company (annual
            refresh) instead of only filling in missing ones.
        tickers: Optional subset of the universe to ingest; the whole
            universe for that date when omitted.

    Returns:
        A message with the new run id, or a diagnostic explaining why no run
        was started (bad date, unknown tickers, missing API key, or another
        run already in flight).
    """
    from agent.ingestion import _default_profile_builder, run_daily_ingest_with_pool

    if not _valid_date(date):
        return f"Invalid date '{date}': expected YYYY-MM-DD."

    universe, unknown = _universe_for(date, tickers)
    if universe.height == 0:
        return f"No universe entries on {date} for: {', '.join(unknown or tickers or [])}."

    try:
        llm = _build_llm()
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        return f"Cannot start ingest: {exc}"

    def work(progress: jobs.ProgressCallback) -> dict[str, int]:
        return asyncio.run(
            run_daily_ingest_with_pool(
                date,
                universe=universe,
                llm=llm,
                profile_builder=_default_profile_builder(llm),
                backfill=backfill,
                profiles_only=profiles_only,
                progress=progress,
            )
        )

    try:
        run_id = jobs.start_run("news", work)
    except jobs.RunInProgress as exc:
        return f"{exc}; refusing to start a second."

    message = (
        f"Ingest started for {date} "
        f"(run_id={run_id}, {universe.height} tickers). Poll get_ingest_status()."
    )
    if unknown:
        message += f" Unknown tickers ignored: {', '.join(unknown)}."
    return message
```

- [ ] Replace `get_ingest_status` (currently around line 1148-1177) with:

```python
@mcp.tool()
async def get_ingest_status(limit: int = 5) -> dict[str, object]:
    """Report the in-flight job run, if any, and recent run history.

    Args:
        limit: Number of past runs to return from the audit log.

    Returns:
        A dict with two keys. 'run' describes the most recent run started
        through this server — run_id, job_type, state ('running',
        'succeeded' or 'failed'), started_at, finished_at, elapsed_seconds,
        tickers_done, tickers_total, counts, error — or None if this server
        has started none. 'history' holds the most recent agent_runs rows
        from PostgreSQL, newest first — run_id, run_date, job_type, status,
        started_at, finished_at, counters (a nested dict of per-job counts,
        not flattened into fixed keys), error. Only 'news'-job runs appear
        here today; 'filings' runs are still logged to a separate DuckDB
        table pending its own cutover, so they will not show up in this
        history until that lands.
    """
    record = jobs.current_run()
    pool = await _pg_pool()
    async with pool.connection() as conn:
        rows = await list_recent_runs(conn, limit=limit)
    return {
        "run": record.snapshot() if record else None,
        "history": [_jsonify_pg_row(row) for row in rows],
    }
```

- [ ] Verify `_rows_to_dicts` and `AGENT_RUN_COLUMNS` have no other
  references left in the file:

```bash
grep -n "AGENT_RUN_COLUMNS" server/mcp.py
```

Expected: no output.

### Step 2: Rewrite the news-job portion of `tests/test_mcp_jobs.py`

- [ ] Replace the full contents of `tests/test_mcp_jobs.py` with:

```python
"""Tests for the MCP job-control tools (background runs, no live calls)."""

from __future__ import annotations

import datetime as dt
import json
import threading

import polars as pl
import pytest

import server.jobs as jobs
import server.mcp as mcp_mod
from data.db.facts import get_current_facts
from data.db.identifiers import get_or_create_identifier
from data.db.init import init_db
from data.sources.rss import Article

pytestmark = pytest.mark.asyncio

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


@pytest.fixture(autouse=True)
def _clear_slot(monkeypatch) -> None:
    """Give every test an empty run slot."""
    monkeypatch.setattr(jobs, "_CURRENT", None)


@pytest.fixture(autouse=True)
def _offline(monkeypatch) -> None:
    """Nothing in this module may reach the network."""
    monkeypatch.setattr(mcp_mod, "get_universe", lambda date: UNIVERSE)
    monkeypatch.setenv("AI_API_KEY", "test-key")
    # The profile builder reaches Wikipedia (and from there the company site)
    # unless its fetchers are stubbed. Stub the network, keep the real builder.
    monkeypatch.setattr(
        "agent.company_profile.fetch_wikipedia_context",
        lambda name: {"extract": f"{name} designs things.", "page_url": None},
    )


def _fake_llm(messages: list[dict[str, str]]) -> str:
    """Answer the profile prompt and the extraction prompt with canned JSON."""
    user = messages[-1]["content"]
    if "Company Profile Generation" in user:
        return json.dumps(
            {
                "description": "Designs consumer electronics.",
                "products_services": ["hardware"],
                "website_url": None,
                "sources": {"wikipedia": None, "crawled": []},
            }
        )
    if "aggregate" in user:
        return json.dumps(
            [
                {
                    "statement": "Technology margins compress.",
                    "fact_type": "margin",
                    "confidence": 0.6,
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


def _fake_news(ticker: str, name: str) -> list[Article]:
    if ticker != "AAPL":
        return []
    return [
        Article(
            ticker="AAPL",
            url="https://example.com/aapl-1",
            title="Apple raises Q3 guidance",
            summary="Revenue above consensus.",
            source="google-news",
            published_at="2026-08-04T09:00:00+00:00",
        )
    ]


@pytest.fixture()
def wired(tmp_path, monkeypatch, postgres_pool):
    """Point the tools at a temp DuckDB (still needed by filings) and stub
    the news job's network calls. Depending on postgres_pool guarantees the
    shared test schema exists before the news job's background thread opens
    its own pool against the same DSN (it can't share this test's event
    loop/connection)."""
    db_path = tmp_path / "research.duckdb"
    init_db(db_path).close()
    monkeypatch.setattr(mcp_mod, "DB_PATH", db_path)
    monkeypatch.setattr(mcp_mod, "_build_llm", lambda: _fake_llm)
    monkeypatch.setattr("agent.ingestion.fetch_company_news", _fake_news)
    monkeypatch.setattr("agent.ingestion.fetch_company_description", lambda name: "")
    monkeypatch.setattr("agent.ingestion.RAW_NEWS_DIR", tmp_path / "news")
    return db_path


def _join_run(timeout: float = 20.0) -> jobs.RunRecord:
    record = jobs.current_run()
    assert record is not None, "no run was started"
    record.thread.join(timeout=timeout)
    assert not record.thread.is_alive(), "run did not finish in time"
    return record


# --- synchronous refusals: no run is started, the caller gets a diagnostic ---


async def test_run_ingest_rejects_a_malformed_date() -> None:
    result = mcp_mod.run_ingest("04-08-2026")

    assert "Invalid date" in result
    assert jobs.current_run() is None


async def test_run_ingest_rejects_tickers_outside_the_universe() -> None:
    result = mcp_mod.run_ingest(DATE, tickers=["ZZZZ"])

    assert "ZZZZ" in result
    assert "No universe entries" in result
    assert jobs.current_run() is None


async def test_run_ingest_reports_a_missing_api_key_instead_of_starting(monkeypatch) -> None:
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    result = mcp_mod.run_ingest(DATE)

    assert "Cannot start ingest" in result
    assert "API key" in result
    assert jobs.current_run() is None


async def test_run_ingest_refuses_a_second_concurrent_run(wired) -> None:
    release = threading.Event()
    first_id = jobs.start_run("news", lambda progress: release.wait(timeout=5) and {})
    try:
        result = mcp_mod.run_ingest(DATE)
        assert first_id in result
        assert "refusing" in result
    finally:
        release.set()
        _join_run()


# --- the real pipeline, end to end, through the tool ---


async def test_run_ingest_drives_the_real_pipeline(wired, pg_conn) -> None:
    result = mcp_mod.run_ingest(DATE)
    record = _join_run()

    assert "started" in result.lower()
    assert record.run_id in result
    assert record.error is None
    assert record.state == "succeeded"
    assert record.counts["facts_written"] == 1
    assert record.tickers_done == 2
    assert record.tickers_total == 2

    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2020, 1, 1),
        source="test",
        name="Apple Inc",
        country="US",
        sector="Technology",
    )
    facts = await get_current_facts(pg_conn, company_id=company_id, as_of=dt.date(2026, 8, 4))
    assert len(facts) == 1
    assert "consensus" in facts[0]["statement"]

    row = await (
        await pg_conn.execute("SELECT COUNT(*) FROM agent_runs WHERE job_type = 'news'")
    ).fetchone()
    assert row[0] == 1


async def test_run_ingest_honours_a_ticker_subset(wired) -> None:
    mcp_mod.run_ingest(DATE, tickers=["aapl"])
    record = _join_run()

    assert record.state == "succeeded"
    assert record.tickers_total == 1


async def test_run_ingest_names_unknown_tickers_but_runs_the_known_ones(wired) -> None:
    result = mcp_mod.run_ingest(DATE, tickers=["AAPL", "ZZZZ"])
    record = _join_run()

    assert "ZZZZ" in result
    assert record.state == "succeeded"
    assert record.tickers_total == 1


async def test_run_ingest_passes_profiles_only_through(wired) -> None:
    mcp_mod.run_ingest(DATE, profiles_only=True)
    record = _join_run()

    assert record.state == "succeeded"
    assert record.counts["articles_fetched"] == 0
    assert record.counts["profiles_built"] == 2


async def test_an_aborted_run_is_reported_by_the_status_tool(wired, monkeypatch) -> None:
    def exploding_llm(messages: list[dict[str, str]]) -> str:
        raise RuntimeError("provider is down")

    monkeypatch.setattr(mcp_mod, "_build_llm", lambda: exploding_llm)
    monkeypatch.setattr("agent.ingestion.MAX_CONSECUTIVE_FAILURES", 1)

    mcp_mod.run_ingest(DATE)
    record = _join_run()

    assert record.state == "failed"
    assert "consecutive" in record.error

    status = await mcp_mod.get_ingest_status()
    assert status["run"]["state"] == "failed"
    assert "consecutive" in status["run"]["error"]


# --- status ---


async def test_get_ingest_status_reports_history_and_no_live_run(wired) -> None:
    status = await mcp_mod.get_ingest_status()

    assert status["run"] is None
    assert status["history"] == []


async def test_get_ingest_status_reports_a_finished_run_and_its_audit_row(wired) -> None:
    mcp_mod.run_ingest(DATE)
    record = _join_run()

    status = await mcp_mod.get_ingest_status()

    assert status["run"]["run_id"] == record.run_id
    assert status["run"]["state"] == "succeeded"
    assert status["run"]["tickers_done"] == 2
    assert status["run"]["elapsed_seconds"] >= 0.0
    assert len(status["history"]) == 1
    assert status["history"][0]["job_type"] == "news"
    assert status["history"][0]["counters"]["facts_written"] == 1


async def test_get_ingest_status_limits_history(wired, pg_conn) -> None:
    for i in range(3):
        await pg_conn.execute(
            "INSERT INTO agent_runs (run_id, run_date, job_type, started_at, finished_at, status) "
            "VALUES (gen_random_uuid(), %s, 'news', %s, NOW(), 'succeeded')",
            (dt.date(2026, 8, i + 1), dt.datetime(2026, 8, i + 1, 9, 0, tzinfo=dt.UTC)),
        )

    status = await mcp_mod.get_ingest_status(limit=2)

    assert len(status["history"]) == 2


async def test_status_is_readable_while_a_run_holds_the_database(wired) -> None:
    """The status tool checks out its own pooled connection while a run is
    mid-write on a different one."""
    writing = threading.Event()
    release = threading.Event()

    def slow_work(progress: jobs.ProgressCallback) -> dict[str, int]:
        progress(1, 2, {"facts_written": 0})
        writing.set()
        release.wait(timeout=5)
        return {"facts_written": 0}

    jobs.start_run("news", slow_work)
    try:
        assert writing.wait(timeout=5)
        status = await mcp_mod.get_ingest_status()

        assert status["run"]["state"] == "running"
        assert status["run"]["tickers_done"] == 1
        assert status["run"]["finished_at"] is None
    finally:
        release.set()
        _join_run()


# --- filings (unchanged by this cutover -- still DuckDB-audited) ---


class _EmptyEdgarClient:
    """EDGAR client that knows the CIK map but has no new submissions."""

    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        return {"AAPL": {"cik": "0000320193", "name": "Apple Inc."}}

    def submissions(self, cik: str) -> list[object]:
        return []


async def test_run_filings_refresh_drives_the_real_fetch(
    wired, postgres_pool, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mcp_mod, "_build_edgar_client", _EmptyEdgarClient)
    monkeypatch.setattr(mcp_mod, "ROOT_DIR", tmp_path)

    result = mcp_mod.run_filings_refresh(DATE)
    record = _join_run()

    assert record.run_id in result
    assert record.error is None
    assert record.state == "succeeded"
    assert record.counts["filings_fetched"] == 0

    conn = init_db(wired)
    try:
        row = conn.execute("SELECT job_type FROM agent_runs").fetchone()
        assert row == ("filings",)
    finally:
        conn.close()


async def test_run_filings_refresh_defaults_to_today(wired, postgres_pool, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mcp_mod, "_build_edgar_client", _EmptyEdgarClient)
    monkeypatch.setattr(mcp_mod, "ROOT_DIR", tmp_path)

    result = mcp_mod.run_filings_refresh()
    _join_run()

    assert dt.date.today().isoformat() in result


async def test_run_filings_refresh_rejects_a_malformed_date() -> None:
    result = mcp_mod.run_filings_refresh("not-a-date")

    assert "Invalid date" in result
    assert jobs.current_run() is None


async def test_run_filings_refresh_reports_an_empty_universe(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "get_universe", lambda date: UNIVERSE.clear())

    result = mcp_mod.run_filings_refresh(DATE)

    assert "No universe entries" in result
    assert jobs.current_run() is None


async def test_run_filings_refresh_refuses_a_second_concurrent_run() -> None:
    release = threading.Event()
    first_id = jobs.start_run("news", lambda progress: release.wait(timeout=5) and {})
    try:
        result = mcp_mod.run_filings_refresh(DATE)
        assert first_id in result
        assert "refusing" in result
    finally:
        release.set()
        _join_run()


async def test_run_filings_refresh_reports_a_client_it_cannot_build(monkeypatch) -> None:
    def broken_client() -> object:
        raise RuntimeError("no EDGAR user agent configured")

    monkeypatch.setattr(mcp_mod, "_build_edgar_client", broken_client)

    result = mcp_mod.run_filings_refresh(DATE)

    assert "Cannot start filings refresh" in result
    assert "EDGAR" in result
    assert jobs.current_run() is None


# --- the construction seams the tools rely on ---


async def test_build_edgar_client_returns_a_usable_client() -> None:
    from data.sources.sec import EdgardClient

    assert isinstance(mcp_mod._build_edgar_client(), EdgardClient)
```

Note: `test_status_is_readable_while_a_run_holds_the_database` drops the
old DuckDB-lock-contention framing (Postgres's MVCC makes that scenario
moot) and instead verifies the in-memory `jobs.RunRecord` snapshot is
readable through `get_ingest_status` while a run is still in flight, which
is the part of the old test that still matters.

- [ ] **Step 3: Run the tests**

```bash
uv run pytest tests/test_mcp_jobs.py -v
```

Expected: all tests PASS.

- [ ] **Step 4: Lint and format**

```bash
uv run ruff check server/mcp.py tests/test_mcp_jobs.py
uv run ruff format server/mcp.py tests/test_mcp_jobs.py
```

Fix any reported issues, then re-run Step 3.

- [ ] **Step 5: Commit**

```bash
git add server/mcp.py tests/test_mcp_jobs.py
git commit -m "Cut run_ingest/get_ingest_status MCP tools over to PostgreSQL"
```

---

## Task 5: Full verification

**Files:** None modified — verification only.

- [ ] **Step 1: Run the full suite**

```bash
docker compose up -d
uv run pytest tests/ -v --cov=. --cov-report=term-missing
```

Expected: all tests PASS. Check the coverage report for `agent/ingestion.py`
and `server/mcp.py` specifically — CLAUDE.md requires 100% coverage (with
mocked API calls) on `server/mcp.py`; `agent/ingestion.py` doesn't carry
that exact bar but should show no large uncovered blocks in the new code
paths (company resolution, the error/abort branches in `run_daily_ingest`).

- [ ] **Step 2: Lint and format the whole repo**

```bash
uv run ruff check .
uv run ruff format --check .
```

Fix any issues; re-run Step 1 if anything changed.

- [ ] **Step 3: Confirm no stray references to deleted modules**

```bash
grep -rn "agent\.knowledge\|agent\.embeddings\|chromadb\|ChromaVectorStore\|HashEmbeddingFunction" --include="*.py" agent server data tests
```

Expected: no output.

- [ ] **Step 4: Confirm CLAUDE.md's migration-status note still holds**

Re-read `CLAUDE.md`'s "Migration status" section against what actually
changed: `data/db/schema.sql`/`research.duckdb` should still be untouched
(filings + other not-yet-migrated tools still depend on it), and this
phase's changes are additive/cutover-only for the news pipeline. No edit
to CLAUDE.md is expected from this phase — if reality has drifted from
what it currently says, flag it rather than editing it silently.

---
