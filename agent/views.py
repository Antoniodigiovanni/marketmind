"""Market view write + structured extraction job."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable

import duckdb

VALID_SCOPES = ("market", "ticker", "theme")


def save_market_view(
    conn: duckdb.DuckDBPyConnection,
    *,
    as_of: str,
    view_text: str,
    model: str,
    scope: str = "market",
    ticker: str | None = None,
    theme: str | None = None,
    data_cutoff: str | None = None,
    used_fact_ids: list[str] | None = None,
) -> str:
    """Insert a free-text market view into market_views (immutable).

    Args:
        conn: DuckDB connection.
        as_of: Market date (YYYY-MM-DD) the view refers to.
        view_text: Free-text view content, never edited in place.
        model: Model identifier that produced the view.
        scope: 'market' | 'ticker' | 'theme'.
        ticker: Ticker symbol when scope == 'ticker'.
        theme: Theme label when scope == 'theme'.
        data_cutoff: ISO timestamp of the newest data used, if known.
        used_fact_ids: ticker_facts ids that informed this view (provenance).

    Returns:
        New view ID.

    Raises:
        ValueError: On invalid scope, missing scope argument, or bad date.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {VALID_SCOPES}, got {scope!r}")
    if scope == "ticker" and not ticker:
        raise ValueError("ticker is required when scope == 'ticker'")
    if scope == "theme" and not theme:
        raise ValueError("theme is required when scope == 'theme'")
    if scope != "ticker" and ticker:
        raise ValueError("ticker is only allowed when scope == 'ticker'")
    if scope != "theme" and theme:
        raise ValueError("theme is only allowed when scope == 'theme'")

    as_of_date = dt.date.fromisoformat(as_of)
    view_id = uuid.uuid4().hex

    conn.execute(
        """
        INSERT INTO market_views
            (id, as_of, scope, ticker, theme, view_text, model, data_cutoff, used_fact_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [view_id, as_of_date, scope, ticker, theme, view_text, model, data_cutoff, used_fact_ids],
    )
    return view_id


def get_market_views(
    conn: duckdb.DuckDBPyConnection,
    *,
    as_of: str,
    ticker: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return market views as of a date (as_of <= :as_of), newest first.

    Args:
        conn: DuckDB connection.
        as_of: Date string (YYYY-MM-DD). Views referring to this date or earlier.
        ticker: Optional ticker filter (scope == 'ticker' rows only).
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        limit: Maximum number of views to return.

    Returns:
        List of view dicts ordered by as_of descending.

    Raises:
        ValueError: If as_of is not a valid date.
    """
    as_of_date = dt.date.fromisoformat(as_of)

    clauses = ["as_of <= ?"]
    params: list[object] = [as_of_date]
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker)
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT id, as_of, scope, ticker, theme, view_text, model, data_cutoff,
               used_fact_ids, created_at
        FROM market_views
        WHERE {" AND ".join(clauses)}
        ORDER BY as_of DESC, created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    return [
        {
            "id": row[0],
            "as_of": row[1].isoformat(),
            "scope": row[2],
            "ticker": row[3],
            "theme": row[4],
            "view_text": row[5],
            "model": row[6],
            "data_cutoff": row[7].isoformat() if row[7] else None,
            "used_fact_ids": row[8],
            "created_at": row[9].isoformat() if row[9] else None,
        }
        for row in rows
    ]


Extractor = Callable[[str, str, str], dict[str, object]]  # (view_id, view_text, model) -> payload


def _default_extractor(view_id: str, view_text: str, model: str) -> dict[str, object]:
    """Placeholder extractor — LLM wiring lands with roadmap 1.7."""
    raise NotImplementedError(
        f"LLM extractor not wired yet (view {view_id}); see agent/prompts/view_structuring.md"
    )


def structure_views(
    conn: duckdb.DuckDBPyConnection,
    *,
    extractor: Extractor | None = None,
    schema_version: str = "v1",
    extraction_model: str = "claude-sonnet-4-5",
) -> int:
    """Structure free-text views that have no structured row yet.

    Idempotent: views that already have a market_view_structures row are skipped.

    Args:
        conn: DuckDB connection.
        extractor: Callable(view_id, view_text, model) -> JSON-serialisable dict.
            Defaults to a stub that raises NotImplementedError.
        schema_version: Schema version label stored with each extraction.
        extraction_model: Model identifier recorded for provenance.

    Returns:
        Number of views structured in this run.

    Raises:
        NotImplementedError: If no extractor is supplied and the default is used.
    """
    extract = extractor or _default_extractor

    pending = conn.execute(
        """
        SELECT mv.id, mv.view_text, mv.model
        FROM market_views mv
        LEFT JOIN market_view_structures mvs ON mvs.view_id = mv.id
        WHERE mvs.view_id IS NULL
        ORDER BY mv.created_at
        """
    ).fetchall()

    structured = 0
    for view_id, view_text, model in pending:
        payload = extract(view_id, view_text, model)
        conn.execute(
            """
            INSERT INTO market_view_structures (view_id, payload, schema_version, extracted_by)
            VALUES (?, ?, ?, ?)
            """,
            [view_id, json.dumps(payload), schema_version, extraction_model],
        )
        structured += 1

    return structured
