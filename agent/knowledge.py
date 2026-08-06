"""Fact store interface — read/write versioned ticker facts from DuckDB + ChromaDB."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid

import duckdb

VALID_AGGREGATE_DIMENSIONS = ("sector", "geography")


def _tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens for lexical similarity."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def get_current_facts(
    ticker: str,
    as_of: str,
    conn: duckdb.DuckDBPyConnection,
) -> list[dict[str, object]]:
    """Retrieve all facts for a ticker valid as of a given date.

    Point-in-time safe: only returns facts where
    valid_from <= as_of AND (valid_until IS NULL OR valid_until > as_of).

    Args:
        ticker: Ticker symbol.
        as_of: Date string (YYYY-MM-DD). Enforced — never omitted.
        conn: DuckDB connection.

    Returns:
        List of fact dicts with keys: id, fact_type, statement, confidence,
        valid_from, valid_until, source_article.

    Raises:
        ValueError: If as_of is not provided or not a valid date.
    """
    if not as_of:
        raise ValueError(
            "as_of is required — querying facts without a date is a look-ahead violation"
        )
    as_of_date = dt.date.fromisoformat(as_of)

    rows = conn.execute(
        """
        SELECT id, fact_type, statement, confidence,
               valid_from, valid_until, source_article
        FROM ticker_facts
        WHERE ticker = ?
          AND valid_from <= ?
          AND (valid_until IS NULL OR valid_until > ?)
        ORDER BY valid_from DESC
        """,
        [ticker, as_of_date, as_of_date],
    ).fetchall()

    return [
        {
            "id": row[0],
            "fact_type": row[1],
            "statement": row[2],
            "confidence": row[3],
            "valid_from": row[4].isoformat() if row[4] else None,
            "valid_until": row[5].isoformat() if row[5] else None,
            "source_article": row[6],
        }
        for row in rows
    ]


def write_fact(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str,
    statement: str,
    fact_type: str,
    confidence: float,
    valid_from: str,
    source_article_id: str,
    supersedes_id: str | None = None,
    vector_store: object | None = None,
) -> str:
    """Write a new fact to DuckDB and embed it in ChromaDB.

    If supersedes_id is provided, sets valid_until on the superseded fact.
    Embedding is deferred until the ChromaDB wiring lands (TODO(embedding)).

    Args:
        conn: DuckDB connection.
        ticker: Ticker symbol.
        statement: Natural language fact statement.
        fact_type: Free-form category string (e.g. 'guidance', 'management').
        confidence: Agent-assigned confidence score 0.0–1.0.
        valid_from: Date the fact became valid (YYYY-MM-DD).
        source_article_id: FK to articles.id.
        supersedes_id: ID of a prior fact this one replaces, if any.
        vector_store: Optional ChromaVectorStore; when given, the statement is
            embedded and embedding_id is stored on the row.

    Returns:
        ID of the newly created fact row.

    Raises:
        ValueError: On invalid dates or confidence outside [0, 1].
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")
    valid_from_date = dt.date.fromisoformat(valid_from)

    fact_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO ticker_facts
            (id, ticker, fact_type, statement, confidence, valid_from, source_article)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [fact_id, ticker, fact_type, statement, confidence, valid_from_date, source_article_id],
    )

    if supersedes_id:
        conn.execute(
            """
            UPDATE ticker_facts
            SET valid_until = ?
            WHERE id = ? AND valid_until IS NULL
            """,
            [valid_from_date, supersedes_id],
        )

    if vector_store is not None:
        embedding_id = f"fact:{fact_id}"
        vector_store.add(
            ids=[embedding_id],
            documents=[statement],
            metadatas=[{"ticker": ticker}],
        )
        conn.execute(
            "UPDATE ticker_facts SET embedding_id = ? WHERE id = ?",
            [embedding_id, fact_id],
        )
    return fact_id


def search_similar_facts(
    ticker: str,
    query: str,
    as_of: str,
    conn: duckdb.DuckDBPyConnection,
    top_k: int = 5,
    vector_store: object | None = None,
) -> list[dict[str, object]]:
    """Retrieve facts similar to a query, scoped to a ticker as of a date.

    Uses the vector store when provided; otherwise falls back to a
    deterministic lexical (Jaccard) score over the point-in-time current facts.
    Either way only facts valid as of `as_of` are considered.

    Args:
        ticker: Ticker symbol to scope the search.
        query: Natural language query or article excerpt.
        as_of: Date string (YYYY-MM-DD) — point-in-time filter.
        conn: DuckDB connection.
        top_k: Number of nearest neighbours to return.
        vector_store: Optional ChromaVectorStore with ticker metadata.

    Returns:
        List of fact dicts ordered by similarity descending, each with an
        added `score` key.

    Raises:
        ValueError: If as_of is not provided or not a valid date.
    """
    if not as_of:
        raise ValueError(
            "as_of is required — querying facts without a date is a look-ahead violation"
        )
    dt.date.fromisoformat(as_of)

    if vector_store is not None:
        hits = vector_store.query([query], n_results=top_k, where={"ticker": ticker})
        similar: list[dict[str, object]] = []
        for hit in hits[0]:
            row = conn.execute(
                "SELECT id, fact_type, statement, confidence FROM ticker_facts WHERE embedding_id = ?",
                [hit["id"]],
            ).fetchone()
            if row is not None:
                similar.append(
                    {
                        "id": row[0],
                        "fact_type": row[1],
                        "statement": row[2],
                        "score": hit["score"],
                    }
                )
        return similar

    query_tokens = _tokens(query)
    scored: list[tuple[float, dict[str, object]]] = []
    for fact in get_current_facts(ticker, as_of, conn):
        fact_tokens = _tokens(str(fact["statement"]))
        union = query_tokens | fact_tokens
        if not union:
            continue
        score = len(query_tokens & fact_tokens) / len(union)
        if score > 0:
            scored.append((score, fact))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "id": fact["id"],
            "fact_type": fact["fact_type"],
            "statement": fact["statement"],
            "score": score,
        }
        for score, fact in scored[:top_k]
    ]


def upsert_company(
    conn: duckdb.DuckDBPyConnection,
    *,
    ticker: str,
    name: str,
    country: str | None = None,
    sector: str | None = None,
    description: str = "",
    description_source: str | None = None,
) -> None:
    """Insert or refresh the cached company metadata row.

    Existing country/sector values are preserved when the new row carries
    NULLs; description and source are always overwritten.
    """
    conn.execute(
        """
        INSERT INTO companies
            (ticker, name, country, sector, description, description_source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker) DO UPDATE SET
            name = excluded.name,
            country = COALESCE(excluded.country, companies.country),
            sector = COALESCE(excluded.sector, companies.sector),
            description = excluded.description,
            description_source = excluded.description_source,
            updated_at = NOW()
        """,
        [ticker, name, country, sector, description, description_source],
    )


def get_company_description(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
) -> str | None:
    """Return the cached company description for a ticker, or None."""
    row = conn.execute(
        "SELECT description FROM companies WHERE ticker = ?",
        [ticker],
    ).fetchone()
    return row[0] if row else None


def write_aggregate_fact(
    conn: duckdb.DuckDBPyConnection,
    *,
    dimension: str,
    dimension_key: str,
    statement: str,
    fact_type: str,
    confidence: float,
    valid_from: str,
    evidence: list[str],
    supersedes_id: str | None = None,
    vector_store: object | None = None,
) -> str:
    """Write a versioned aggregate fact (sector or geography).

    If supersedes_id is provided, sets valid_until on the superseded row.

    Args:
        conn: DuckDB connection.
        dimension: 'sector' | 'geography'.
        dimension_key: Dimension value, e.g. 'Technology' or 'US'.
        statement: Natural language aggregate statement.
        fact_type: Free-form category string.
        confidence: Agent-assigned confidence 0.0–1.0.
        valid_from: Date the fact became valid (YYYY-MM-DD).
        evidence: ticker_facts ids supporting this aggregate.
        supersedes_id: Prior aggregate fact this one replaces, if any.
        vector_store: Optional vector store; embeds when given.

    Returns:
        ID of the newly created aggregate fact row.

    Raises:
        ValueError: On invalid dimension, dates, or confidence.
    """
    if dimension not in VALID_AGGREGATE_DIMENSIONS:
        raise ValueError(
            f"dimension must be one of {VALID_AGGREGATE_DIMENSIONS}, got {dimension!r}"
        )
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")
    valid_from_date = dt.date.fromisoformat(valid_from)

    fact_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO aggregate_facts
            (id, dimension, dimension_key, fact_type, statement, confidence,
             valid_from, evidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            fact_id,
            dimension,
            dimension_key,
            fact_type,
            statement,
            confidence,
            valid_from_date,
            json.dumps(evidence),
        ],
    )

    if supersedes_id:
        conn.execute(
            """
            UPDATE aggregate_facts
            SET valid_until = ?
            WHERE id = ? AND valid_until IS NULL
            """,
            [valid_from_date, supersedes_id],
        )

    if vector_store is not None:
        embedding_id = f"agg:{fact_id}"
        vector_store.add(
            ids=[embedding_id],
            documents=[statement],
            metadatas=[{"dimension": dimension, "dimension_key": dimension_key}],
        )
        conn.execute(
            "UPDATE aggregate_facts SET embedding_id = ? WHERE id = ?",
            [embedding_id, fact_id],
        )
    return fact_id


def get_current_aggregate_facts(
    dimension: str,
    dimension_key: str,
    as_of: str,
    conn: duckdb.DuckDBPyConnection,
) -> list[dict[str, object]]:
    """Return aggregate facts valid for a dimension key as of a date.

    Point-in-time safe: only facts where valid_from <= as_of AND
    (valid_until IS NULL OR valid_until > as_of) are returned.

    Args:
        dimension: 'sector' | 'geography'.
        dimension_key: Dimension value, e.g. 'Technology' or 'US'.
        as_of: Date string (YYYY-MM-DD).
        conn: DuckDB connection.

    Returns:
        List of aggregate fact dicts ordered by valid_from descending.

    Raises:
        ValueError: If as_of is missing/invalid or dimension is invalid.
    """
    if dimension not in VALID_AGGREGATE_DIMENSIONS:
        raise ValueError(
            f"dimension must be one of {VALID_AGGREGATE_DIMENSIONS}, got {dimension!r}"
        )
    if not as_of:
        raise ValueError(
            "as_of is required — querying facts without a date is a look-ahead violation"
        )
    as_of_date = dt.date.fromisoformat(as_of)

    rows = conn.execute(
        """
        SELECT id, dimension, dimension_key, fact_type, statement, confidence,
               valid_from, valid_until, evidence
        FROM aggregate_facts
        WHERE dimension = ?
          AND dimension_key = ?
          AND valid_from <= ?
          AND (valid_until IS NULL OR valid_until > ?)
        ORDER BY valid_from DESC
        """,
        [dimension, dimension_key, as_of_date, as_of_date],
    ).fetchall()

    return [
        {
            "id": row[0],
            "dimension": row[1],
            "dimension_key": row[2],
            "fact_type": row[3],
            "statement": row[4],
            "confidence": row[5],
            "valid_from": row[6].isoformat() if row[6] else None,
            "valid_until": row[7].isoformat() if row[7] else None,
            "evidence": json.loads(row[8]) if row[8] else [],
        }
        for row in rows
    ]
