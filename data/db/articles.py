"""Article repository — global content dedup, per-company attribution and status."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.rows import dict_row


async def register_article(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    url_hash: str,
    content_hash: str,
    url: str,
    source: str,
    publisher: str | None = None,
    title: str | None,
    published_at: dt.datetime,
    pre_score: float,
    summary: str | None = None,
) -> uuid.UUID | None:
    """Register an article for a company.

    `articles` is global, deduped on url_hash/content_hash: the same story
    is one row regardless of how many companies' feeds surface it.
    `article_companies` is per company: a story already seen by one company
    is still processed for a second, rather than being dropped globally
    (audit A1.2) -- the two-argument dedup below is what makes that true.

    Args:
        conn: An open connection.
        company_id: The company whose feed surfaced this article.
        url_hash: Content dedup key.
        content_hash: Content dedup key.
        url: Article URL.
        source: Provider, e.g. 'google-news', 'yahoo-finance', 'sec:8-K'.
        publisher: Original publisher when supplied by the feed.
        title: Article title, if known.
        published_at: Publication timestamp.
        pre_score: source_credibility * recency decay, computed by the caller.

    Returns:
        The article_id to process, or None if this (article, company) pair
        has already been registered -- whether the article itself is new
        or was already known for other companies.
    """
    async with conn.transaction():
        existing = await (
            await conn.execute(
                "SELECT article_id FROM articles WHERE url_hash = %s OR content_hash = %s",
                (url_hash, content_hash),
            )
        ).fetchone()
        if existing is not None:
            article_id = existing[0]
        else:
            row = await (
                await conn.execute(
                    "INSERT INTO articles "
                    "(url_hash, content_hash, url, source, publisher, title, summary, published_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING article_id",
                    (url_hash, content_hash, url, source, publisher, title, summary, published_at),
                )
            ).fetchone()
            article_id = row[0]

        inserted = await (
            await conn.execute(
                "INSERT INTO article_companies (article_id, company_id, pre_score) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (article_id, company_id) DO NOTHING "
                "RETURNING article_id",
                (article_id, company_id, pre_score),
            )
        ).fetchone()

    return article_id if inserted is not None else None


async def set_article_status(
    conn: psycopg.AsyncConnection,
    *,
    article_id: uuid.UUID,
    company_id: uuid.UUID,
    status: str,
    error: str | None = None,
) -> None:
    """Update an article's per-company processing status.

    Every call counts as an attempt -- attempts increments regardless of
    the resulting status, so it answers "how many times has this pair been
    processed", not just "how many times has it failed".

    Args:
        conn: An open connection.
        article_id: The article.
        company_id: The company this status applies to.
        status: 'pending', 'processed', 'redundant', 'filtered', or 'error'.
        error: Failure reason when status='error'; None otherwise.
    """
    async with conn.transaction():
        await conn.execute(
            "UPDATE article_companies SET status = %s, error = %s, "
            "attempts = attempts + 1, updated_at = NOW() "
            "WHERE article_id = %s AND company_id = %s",
            (status, error, article_id, company_id),
        )


async def get_feed_checkpoints(
    conn: psycopg.AsyncConnection, *, company_id: uuid.UUID
) -> dict[str, dt.datetime]:
    """Return the per-source publication high-water marks for a company.

    Args:
        conn: An open connection.
        company_id: Company whose feed checkpoints are requested.

    Returns:
        Mapping of feed source to its latest processed publication timestamp.
    """
    rows = await (
        await conn.execute(
            "SELECT SUBSTRING(stage FROM 6) AS source, MAX(last_published_at) "
            "FROM ingest_checkpoints WHERE company_id = %s AND stage LIKE 'feed:%%' "
            "AND status = 'done' AND last_published_at IS NOT NULL GROUP BY stage",
            (company_id,),
        )
    ).fetchall()
    return {str(source): published_at for source, published_at in rows}


async def update_feed_checkpoints(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    run_date: dt.date,
    published_by_source: dict[str, dt.datetime],
) -> None:
    """Advance per-source high-water marks without ever moving them backward.

    Args:
        conn: An open connection.
        company_id: Company whose feed checkpoints are advanced.
        run_date: Ingest date owning these checkpoint rows.
        published_by_source: Maximum successfully handled timestamp per source.
    """
    if not published_by_source:
        return
    async with conn.transaction(), conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO ingest_checkpoints "
            "(run_date, company_id, stage, status, last_published_at) "
            "VALUES (%s, %s, %s, 'done', %s) "
            "ON CONFLICT (run_date, company_id, stage) DO UPDATE SET "
            "status = 'done', last_published_at = GREATEST("
            "ingest_checkpoints.last_published_at, EXCLUDED.last_published_at), "
            "updated_at = NOW()",
            [
                (run_date, company_id, f"feed:{source}", published_at)
                for source, published_at in published_by_source.items()
            ],
        )


async def get_news(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    since: dt.datetime | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return articles attributed to a company, newest first."""
    query = (
        "SELECT a.url, a.source, a.publisher, i.id_value AS ticker, a.published_at, "
        "a.title, a.summary "
        "FROM articles a JOIN article_companies ac USING (article_id) "
        "LEFT JOIN company_identifiers i ON i.company_id = ac.company_id "
        "AND i.id_type = 'ticker' AND i.valid_until IS NULL "
        "WHERE ac.company_id = %s"
    )
    params: list[object] = [company_id]
    if since is not None:
        query += " AND a.published_at >= %s"
        params.append(since)
    query += " ORDER BY a.published_at DESC LIMIT %s"
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def search_news(
    conn: psycopg.AsyncConnection, *, query: str, limit: int = 50
) -> list[dict[str, object]]:
    """Search article titles and summaries, newest first."""
    pattern = f"%{query}%"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT DISTINCT ON (a.article_id) a.url, a.source, a.publisher, "
            "i.id_value AS ticker, a.published_at, a.title, a.summary FROM articles a "
            "LEFT JOIN article_companies ac USING (article_id) "
            "LEFT JOIN company_identifiers i ON i.company_id = ac.company_id "
            "AND i.id_type = 'ticker' AND i.valid_until IS NULL "
            "WHERE a.title ILIKE %s OR a.summary ILIKE %s "
            "ORDER BY a.article_id, a.published_at DESC LIMIT %s",
            (pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    return sorted(rows, key=lambda row: row["published_at"], reverse=True)
