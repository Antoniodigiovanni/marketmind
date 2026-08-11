"""Aggregate fact repository — sector/geography facts, evidence, and their
pgvector embeddings.

Same point-in-time and embed-then-write-transaction shape as
data/db/facts.py, plus an evidence join: which company_facts rows back a
given aggregate fact.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import psycopg
from psycopg.rows import dict_row

VALID_DIMENSIONS = frozenset({"sector", "geography"})


async def create_aggregate_fact(
    conn: psycopg.AsyncConnection,
    *,
    dimension: str,
    dimension_key: str,
    statement: str,
    fact_type: str,
    confidence: float,
    valid_from: dt.date,
    evidence: list[uuid.UUID],
    embed: Callable[[str], Awaitable[list[float]]],
    model: str,
    supersedes_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Write a new aggregate fact, its embedding, and its evidence links.

    Args:
        conn: An open connection.
        dimension: 'sector' or 'geography'.
        dimension_key: Dimension value, e.g. 'Technology' or 'US'.
        statement: Natural language aggregate statement.
        fact_type: Free-form category string.
        confidence: Agent-assigned confidence score, must be in [0, 1].
        valid_from: Date the fact became valid.
        evidence: company_facts.fact_id rows supporting this aggregate.
        embed: Async callable producing the statement's embedding vector.
            Called before any database write, so a failing embedder leaves
            no trace.
        model: Identifies which embedder produced the vector.
        supersedes_id: A prior aggregate fact this one replaces, if any.
            That row's valid_until is set to this fact's valid_from --
            aggregate_facts has no supersedes_id column to link through,
            unlike company_facts and company_profiles.

    Returns:
        The new aggregate_id.

    Raises:
        ValueError: If dimension is not 'sector'/'geography', or confidence
            is outside [0, 1]. Both checked before calling embed.
    """
    if dimension not in VALID_DIMENSIONS:
        raise ValueError(f"dimension must be one of {sorted(VALID_DIMENSIONS)}, got {dimension!r}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")

    vector = await embed(statement)

    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO aggregate_facts "
                "(dimension, dimension_key, fact_type, statement, confidence, valid_from) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING aggregate_id",
                (dimension, dimension_key, fact_type, statement, confidence, valid_from),
            )
        ).fetchone()
        aggregate_id = row[0]

        await conn.execute(
            "INSERT INTO aggregate_fact_embeddings (aggregate_id, model, embedding) "
            "VALUES (%s, %s, %s)",
            (aggregate_id, model, vector),
        )

        for fact_id in evidence:
            await conn.execute(
                "INSERT INTO aggregate_fact_evidence (aggregate_id, fact_id) VALUES (%s, %s)",
                (aggregate_id, fact_id),
            )

        if supersedes_id is not None:
            await conn.execute(
                "UPDATE aggregate_facts SET valid_until = %s "
                "WHERE aggregate_id = %s AND valid_until IS NULL",
                (valid_from, supersedes_id),
            )

    return aggregate_id


async def get_current_aggregate_facts(
    conn: psycopg.AsyncConnection,
    *,
    dimension: str,
    dimension_key: str,
    as_of: dt.date,
) -> list[dict[str, object]]:
    """Retrieve aggregate facts for a dimension key valid as of a date.

    Args:
        conn: An open connection.
        dimension: 'sector' or 'geography'.
        dimension_key: Dimension value, e.g. 'Technology' or 'US'.
        as_of: The date to resolve as of.

    Returns:
        One dict per fact: aggregate_id, dimension, dimension_key,
        fact_type, statement, confidence, valid_from, valid_until,
        evidence (list of company_facts.fact_id, empty list if none),
        ordered by valid_from descending.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT af.aggregate_id, af.dimension, af.dimension_key, af.fact_type,
                   af.statement, af.confidence, af.valid_from, af.valid_until,
                   COALESCE(
                       array_agg(afe.fact_id) FILTER (WHERE afe.fact_id IS NOT NULL),
                       '{}'
                   ) AS evidence
            FROM aggregate_facts af
            LEFT JOIN aggregate_fact_evidence afe ON afe.aggregate_id = af.aggregate_id
            WHERE af.dimension = %s AND af.dimension_key = %s
              AND af.valid_from <= %s
              AND (af.valid_until IS NULL OR af.valid_until > %s)
            GROUP BY af.aggregate_id, af.dimension, af.dimension_key, af.fact_type,
                     af.statement, af.confidence, af.valid_from, af.valid_until
            ORDER BY af.valid_from DESC
            """,
            (dimension, dimension_key, as_of, as_of),
        )
        return await cur.fetchall()


async def search_similar_aggregate_facts(
    conn: psycopg.AsyncConnection,
    *,
    dimension: str,
    dimension_key: str,
    query: str,
    as_of: dt.date,
    embed: Callable[[str], Awaitable[list[float]]],
    top_k: int = 5,
) -> list[dict[str, object]]:
    """Retrieve aggregate facts similar to a query, scoped and as-of dated.

    Args:
        conn: An open connection.
        dimension: 'sector' or 'geography'.
        dimension_key: Dimension value to scope the search to.
        query: Natural language query.
        as_of: The date to resolve as of.
        embed: Async callable producing the query's embedding vector.
        top_k: Number of nearest neighbours to return.

    Returns:
        One dict per fact: aggregate_id, fact_type, statement, similarity,
        ordered by similarity descending.
    """
    query_vector = await embed(query)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT af.aggregate_id, af.fact_type, af.statement,
                   1 - (e.embedding <=> %s::vector) AS similarity
            FROM aggregate_facts af
            JOIN aggregate_fact_embeddings e ON e.aggregate_id = af.aggregate_id
            WHERE af.dimension = %s AND af.dimension_key = %s
              AND af.valid_from <= %s
              AND (af.valid_until IS NULL OR af.valid_until > %s)
            ORDER BY e.embedding <=> %s::vector
            LIMIT %s
            """,
            (query_vector, dimension, dimension_key, as_of, as_of, query_vector, top_k),
        )
        return await cur.fetchall()
