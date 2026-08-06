"""Daily company-level news → facts pipeline with sector/geography aggregates."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from loguru import logger

from agent.embeddings import ChromaVectorStore, HashEmbeddingFunction
from agent.knowledge import (
    get_company_description,
    get_current_facts,
    search_similar_facts,
    upsert_company,
    write_aggregate_fact,
    write_fact,
)
from agent.llm import ChatClient, parse_json_response
from data.db.init import DEFAULT_DB_PATH, init_db
from data.sources.rss import Article, fetch_company_news
from data.sources.wikipedia import fetch_company_description
from data.universe.loader import get_universe

SOURCE_CREDIBILITY: dict[str, float] = {
    "google-news": 0.55,
    "yahoo-finance": 0.6,
}
RECENCY_LAMBDA = 0.05

RAW_NEWS_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "news"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

NewsFetcher = Callable[[str, str], list[Article]]
DescriptionFetcher = Callable[[str], str]
LLMCallable = Callable[[list[dict[str, str]]], str]


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def register_article(
    conn: duckdb.DuckDBPyConnection,
    article: Article,
    score: float,
) -> str | None:
    """Insert an article unless it is a level-1 dedup hit.

    Args:
        conn: DuckDB connection.
        article: Fetched article.
        score: Pre-LLM score.

    Returns:
        New article id, or None when the url/content hash already exists.
    """
    existing = conn.execute(
        """
        SELECT 1 FROM articles
        WHERE url_hash = ? OR content_hash = ?
        LIMIT 1
        """,
        [article.url_hash, article.content_hash],
    ).fetchone()
    if existing:
        return None

    article_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO articles
            (id, url_hash, content_hash, url, source, ticker,
             published_at, fetched_at, title, pre_score, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        [
            article_id,
            article.url_hash,
            article.content_hash,
            article.url,
            article.source,
            article.ticker,
            article.published_at,
            dt.datetime.now(dt.UTC).isoformat(),
            article.title,
            score,
        ],
    )
    return article_id


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
        "\n".join(f"- [{f['id']}] ({f['fact_type']}) {f['statement']}" for f in current_facts)
        or "(none)"
    )
    similar_block = (
        "\n".join(f"- [{f['id']}] ({f['fact_type']}) {f['statement']}" for f in similar_facts)
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
        current_facts=current_block,
        similar_facts=similar_block,
        pre_score=f"{score:.3f}",
    )
    return [
        {"role": "system", "content": "You are a precise market-research fact extractor."},
        {"role": "user", "content": prompt},
    ]


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


def apply_facts(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str,
    date: str,
    article_id: str,
    facts: list[dict[str, Any]],
    current_facts: list[dict[str, object]],
    vector_store: object | None,
) -> tuple[int, int, list[dict[str, str]]]:
    """Write extracted facts, retiring superseded ones.

    Returns:
        (facts written, facts superseded, written fact rows for aggregation).
    """
    statements = {str(f["statement"]): f for f in current_facts}
    written = 0
    superseded = 0
    rows: list[dict[str, str]] = []
    for fact in facts:
        supersede_stmt = str(fact.get("supersedes_statement") or "").strip()
        supersedes_id = statements[supersede_stmt]["id"] if supersede_stmt in statements else None
        fact_id = write_fact(
            conn,
            ticker=ticker,
            statement=str(fact["statement"]),
            fact_type=str(fact.get("fact_type") or "general"),
            confidence=float(fact["confidence"]),
            valid_from=date,
            source_article_id=article_id,
            supersedes_id=supersedes_id,
            vector_store=vector_store,
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


def _set_article_status(
    conn: duckdb.DuckDBPyConnection,
    article_id: str,
    status: str,
) -> None:
    """Update an article's processing status."""
    conn.execute(
        "UPDATE articles SET status = ? WHERE id = ?",
        [status, article_id],
    )


def _write_raw_news(
    articles: list[Article],
    date: str,
    raw_news_dir: Path,
) -> Path:
    """Write fetched articles to an immutable NDJSON file for the date."""
    raw_news_dir.mkdir(parents=True, exist_ok=True)
    path = raw_news_dir / f"{date}.ndjson"
    if path.exists():
        return path
    with path.open("w", encoding="utf-8") as handle:
        for article in articles:
            handle.write(
                json.dumps(
                    {
                        "ticker": article.ticker,
                        "url": article.url,
                        "url_hash": article.url_hash,
                        "title": article.title,
                        "summary": article.summary,
                        "source": article.source,
                        "published_at": article.published_at,
                        "content_hash": article.content_hash,
                    }
                )
                + "\n"
            )
    return path


def run_daily_ingest(
    date: str,
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
    universe: pl.DataFrame | None = None,
    news_fetcher: NewsFetcher | None = None,
    description_fetcher: DescriptionFetcher | None = None,
    llm: LLMCallable | None = None,
    vector_store: object | None = None,
    raw_news_dir: Path | None = None,
) -> dict[str, int]:
    """Run the full daily news → facts pipeline.

    Steps:
    1. Load the point-in-time universe for the date.
    2. Per company: cache a description, fetch news (RSS, keyless).
    3. Level-1 dedup against the articles table (url/content hash).
    4. Pre-score each article.
    5. Per new article: retrieve current + similar past facts, call the model,
       and write (or retire) facts.
    6. Aggregate the day's new facts by sector and country into
       aggregate_facts.
    7. Write raw news NDJSON and an agent_runs audit row.

    Args:
        date: Date string (YYYY-MM-DD).
        conn: Optional DuckDB connection (defaults to research.duckdb).
        universe: Optional point-in-time universe frame (defaults to loader).
        news_fetcher: Callable(ticker, name) -> list[Article] (defaults to RSS).
        description_fetcher: Callable(name) -> str (defaults to Wikipedia).
        llm: Callable(messages) -> text; required.
        vector_store: Optional ChromaVectorStore for embedding past facts.
        raw_news_dir: Directory for the immutable per-date NDJSON file.

    Returns:
        Summary counts keyed by articles_fetched, articles_skipped,
        articles_redundant, articles_errored, facts_written,
        facts_superseded, aggregate_written.

    Raises:
        ValueError: On an invalid date or a missing llm callable.
    """
    dt.date.fromisoformat(date)
    if llm is None:
        raise ValueError("llm callable is required")
    description_fetcher = description_fetcher or fetch_company_description

    owns_conn = conn is None
    if conn is None:
        conn = init_db(DEFAULT_DB_PATH)
    universe = universe if universe is not None else get_universe(date)
    raw_news_dir = raw_news_dir or RAW_NEWS_DIR

    started_at = dt.datetime.now(dt.UTC).isoformat()
    run_id = uuid.uuid4().hex
    summary = {
        "articles_fetched": 0,
        "articles_skipped": 0,
        "articles_redundant": 0,
        "articles_errored": 0,
        "facts_written": 0,
        "facts_superseded": 0,
        "aggregate_written": 0,
    }
    all_articles: list[Article] = []
    todays_facts: list[dict[str, str | None]] = []

    try:
        for row in universe.iter_rows(named=True):
            ticker = str(row["ticker"])
            name = str(row["name"])
            country = row.get("country")
            sector = row.get("sector")

            description = get_company_description(conn, ticker)
            if description is None:
                try:
                    description = description_fetcher(name)
                except Exception as exc:  # noqa: BLE001 - one company must not sink the run
                    logger.warning(f"description fetch failed for {ticker}: {exc}")
                    description = ""
                upsert_company(
                    conn,
                    ticker=ticker,
                    name=name,
                    country=str(country) if country else None,
                    sector=str(sector) if sector else None,
                    description=description,
                    description_source="wikipedia" if description else None,
                )

            try:
                articles = (
                    news_fetcher(ticker, name)
                    if news_fetcher is not None
                    else fetch_company_news(ticker, name)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"news fetch failed for {ticker}: {exc}")
                articles = []
            all_articles.extend(articles)
            summary["articles_fetched"] += len(articles)

            for article in articles:
                score = pre_score(article.source, article.published_at, date)
                article_id = register_article(conn, article, score)
                if article_id is None:
                    summary["articles_skipped"] += 1
                    continue

                current_facts = get_current_facts(ticker, date, conn)
                similar_facts = search_similar_facts(
                    ticker,
                    f"{article.title} {article.summary}",
                    date,
                    conn,
                    vector_store=vector_store,
                )
                messages = build_extraction_messages(
                    article=article,
                    company_name=name,
                    description=description or "",
                    sector=str(sector) if sector else None,
                    country=str(country) if country else None,
                    current_facts=current_facts,
                    similar_facts=similar_facts,
                    score=score,
                    date=date,
                )
                try:
                    parsed = parse_fact_list_response(llm(messages))
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"extraction failed for article {article_id}: {exc}")
                    _set_article_status(conn, article_id, "error")
                    summary["articles_errored"] += 1
                    continue

                if parsed is None:
                    _set_article_status(conn, article_id, "redundant")
                    summary["articles_redundant"] += 1
                    continue

                written, superseded, fact_rows = apply_facts(
                    conn,
                    ticker=ticker,
                    date=date,
                    article_id=article_id,
                    facts=parsed,
                    current_facts=current_facts,
                    vector_store=vector_store,
                )
                _set_article_status(conn, article_id, "processed")
                summary["facts_written"] += written
                summary["facts_superseded"] += superseded
                for fact_row in fact_rows:
                    todays_facts.append(
                        {
                            "ticker": fact_row["ticker"],
                            "fact_id": fact_row["fact_id"],
                            "fact_type": fact_row["fact_type"],
                            "statement": fact_row["statement"],
                            "sector": str(sector) if sector else None,
                            "country": str(country) if country else None,
                        }
                    )

        _write_raw_news(all_articles, date, raw_news_dir)

        for dimension, key_field in (("sector", "sector"), ("geography", "country")):
            groups: dict[str, list[dict[str, str | None]]] = {}
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
                evidence = [str(f["fact_id"]) for f in group_facts]
                for item in parsed:
                    write_aggregate_fact(
                        conn,
                        dimension=dimension,
                        dimension_key=dimension_key,
                        statement=str(item["statement"]),
                        fact_type=str(item.get("fact_type") or "general"),
                        confidence=float(item["confidence"]),
                        valid_from=date,
                        evidence=evidence,
                        vector_store=vector_store,
                    )
                    summary["aggregate_written"] += 1

        conn.execute(
            """
            INSERT INTO agent_runs
                (id, run_date, job_type, started_at, finished_at,
                 articles_fetched, articles_skipped, articles_redundant,
                 facts_written, facts_superseded)
            VALUES (?, ?, 'news', ?, NOW(), ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                dt.date.fromisoformat(date),
                started_at,
                summary["articles_fetched"],
                summary["articles_skipped"],
                summary["articles_redundant"],
                summary["facts_written"],
                summary["facts_superseded"],
            ],
        )
    finally:
        if owns_conn:
            conn.close()

    return summary


def main() -> None:
    """Run the daily ingest job from the command line."""
    parser = argparse.ArgumentParser(description="Daily news → facts ingest")
    parser.add_argument("date", help="YYYY-MM-DD")
    args = parser.parse_args()

    client = ChatClient()
    vector_store = ChromaVectorStore(embedding_function=HashEmbeddingFunction())
    summary = run_daily_ingest(
        args.date,
        llm=client.complete,
        vector_store=vector_store,
    )
    logger.info(f"ingest summary for {args.date}: {summary}")


if __name__ == "__main__":
    main()
