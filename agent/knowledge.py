"""Fact store interface — read/write versioned ticker facts from DuckDB + ChromaDB."""

from __future__ import annotations

import datetime as dt
import uuid

import duckdb


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

    # TODO(embedding): embed statement and store in ChromaDB keyed by embedding_id.
    return fact_id


def search_similar_facts(ticker: str, query: str, top_k: int = 5) -> list[dict[str, object]]:
    """Retrieve facts semantically similar to a query string via ChromaDB.

    Args:
        ticker: Ticker symbol to scope the search.
        query: Natural language query or article excerpt.
        top_k: Number of nearest neighbours to return.

    Returns:
        List of fact dicts ordered by cosine similarity descending.

    Raises:
        NotImplementedError: Until the ChromaDB wiring lands (roadmap 1.5).
    """
    raise NotImplementedError("ChromaDB search lands with the embedding wiring in roadmap 1.5")
