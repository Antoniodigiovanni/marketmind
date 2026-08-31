"""Company facts repository — point-in-time facts and their pgvector embeddings.

Every read is as-of dated (CLAUDE.md's fact-store convention: querying
without a date is a look-ahead violation). Every write embeds; pgvector is the
only vector store.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import psycopg
from psycopg.rows import dict_row


async def create_fact(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    statement: str,
    fact_type: str,
    confidence: float,
    valid_from: dt.date,
    source_article_id: uuid.UUID,
    embed: Callable[[str], Awaitable[list[float]]],
    model: str,
    supersedes_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Write a new fact and its embedding in one transaction.

    Args:
        conn: An open connection.
        company_id: The fact's subject.
        statement: Natural language fact statement.
        fact_type: Free-form category string (e.g. 'guidance', 'management').
        confidence: Agent-assigned confidence score, must be in [0, 1].
        valid_from: Date the fact became valid.
        source_article_id: FK to articles.article_id.
        embed: Async callable producing the statement's embedding vector.
            Called before any database write, so a failing embedder leaves
            no trace -- no fact row, nothing to roll back.
        model: Identifies which embedder produced the vector (stored on
            fact_embeddings.model), e.g. 'hash-ngram'.
        supersedes_id: A prior fact this one replaces, if any. That row's
            valid_until is set to this fact's valid_from.

    Returns:
        The new fact_id.

    Raises:
        ValueError: If confidence is outside [0, 1]. Checked before calling
            embed, so an invalid write never pays for an embedding call.
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")

    vector = await embed(statement)

    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO company_facts "
                "(company_id, fact_type, statement, confidence, valid_from, "
                "supersedes_id, source_article) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING fact_id",
                (
                    company_id,
                    fact_type,
                    statement,
                    confidence,
                    valid_from,
                    supersedes_id,
                    source_article_id,
                ),
            )
        ).fetchone()
        fact_id = row[0]

        await conn.execute(
            "INSERT INTO fact_embeddings (fact_id, model, embedding) VALUES (%s, %s, %s)",
            (fact_id, model, vector),
        )

        if supersedes_id is not None:
            await conn.execute(
                "UPDATE company_facts SET valid_until = %s "
                "WHERE fact_id = %s AND valid_until IS NULL",
                (valid_from, supersedes_id),
            )

    return fact_id


async def get_current_facts(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    as_of: dt.date,
) -> list[dict[str, object]]:
    """Retrieve all facts for a company valid as of a date.

    Args:
        conn: An open connection.
        company_id: The company to fetch facts for.
        as_of: The date to resolve as of.

    Returns:
        One dict per fact: fact_id, fact_type, statement, confidence,
        valid_from, valid_until, source_article, ordered by valid_from
        descending.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT fact_id, fact_type, statement, confidence, valid_from, "
            "valid_until, source_article FROM company_facts "
            "WHERE company_id = %s AND valid_from <= %s "
            "AND (valid_until IS NULL OR valid_until > %s) "
            "ORDER BY valid_from DESC",
            (company_id, as_of, as_of),
        )
        return await cur.fetchall()


async def search_similar_facts(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    query: str,
    as_of: dt.date,
    embed: Callable[[str], Awaitable[list[float]]],
    top_k: int = 5,
) -> list[dict[str, object]]:
    """Retrieve facts similar to a query, scoped to a company as of a date.

    Vector ordering and the point-in-time predicate are one query: a fact
    retired before as_of cannot appear regardless of embedding distance.

    Args:
        conn: An open connection.
        company_id: The company to scope the search to.
        query: Natural language query or article excerpt.
        as_of: The date to resolve as of.
        embed: Async callable producing the query's embedding vector.
        top_k: Number of nearest neighbours to return.

    Returns:
        One dict per fact: fact_id, fact_type, statement, similarity,
        ordered by similarity descending.
    """
    query_vector = await embed(query)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT f.fact_id, f.fact_type, f.statement,
                   1 - (e.embedding <=> %s::vector) AS similarity
            FROM company_facts f
            JOIN fact_embeddings e ON e.fact_id = f.fact_id
            WHERE f.company_id = %s
              AND f.valid_from <= %s
              AND (f.valid_until IS NULL OR f.valid_until > %s)
            ORDER BY e.embedding <=> %s::vector
            LIMIT %s
            """,
            (query_vector, company_id, as_of, as_of, query_vector, top_k),
        )
        return await cur.fetchall()
