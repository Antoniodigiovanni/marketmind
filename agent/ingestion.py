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
from data.db.identifiers import create_identifier, resolve_identifier
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
        "\n".join(f"- [{f['fact_id']}] ({f['fact_type']}) {f['statement']}" for f in current_facts)
        or "(none)"
    )
    similar_block = (
        "\n".join(f"- [{f['fact_id']}] ({f['fact_type']}) {f['statement']}" for f in similar_facts)
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

    Refreshes the cached description (and, for a genuinely new identifier,
    name/country/sector) only the first time a company has no description
    yet, rather than refreshing it on every run.

    When resolve_identifier finds no coverage for `as_of`, this always
    creates a fresh company rather than falling back to whichever company
    currently holds `ticker` -- that fallback would misattribute historical
    facts to an unrelated company and overwrite its live attributes on a
    backfill run for a date before this ticker's identifier existed. See
    create_identifier's docstring.

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
        company_id = await create_identifier(
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
    if description is None:
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
                        products_services=[str(c) for c in built.get("products_services") or []],
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
                        summary=article.summary,
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

                    try:
                        written, superseded, fact_rows = await apply_facts(
                            conn,
                            company_id=company_id,
                            ticker=ticker,
                            date=date_as_date,
                            article_id=article_id,
                            facts=parsed,
                            current_facts=current_facts,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"fact write failed for article {article_id}: {exc}")
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
