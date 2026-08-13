"""Article repository — global content dedup, per-company attribution and status."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg


async def register_article(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    url_hash: str,
    content_hash: str,
    url: str,
    source: str,
    title: str | None,
    published_at: dt.datetime,
    pre_score: float,
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
                    "(url_hash, content_hash, url, source, title, published_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING article_id",
                    (url_hash, content_hash, url, source, title, published_at),
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
        status: 'pending', 'processed', 'redundant', or 'error'.
        error: Failure reason when status='error'; None otherwise.
    """
    async with conn.transaction():
        await conn.execute(
            "UPDATE article_companies SET status = %s, error = %s, "
            "attempts = attempts + 1, updated_at = NOW() "
            "WHERE article_id = %s AND company_id = %s",
            (status, error, article_id, company_id),
        )
