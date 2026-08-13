"""Market views repository — free-text market snapshots and their structured extractions.

Takes `company_id` directly, never `ticker` — ticker resolution is not this
module's job (see `agent/views.py`, which resolves ticker -> company_id via
`data/db/identifiers.py` before calling into this module), matching every
other repository module here (`facts.py`, `aggregate_facts.py` take
`company_id`, not a human-facing identifier).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


async def save_market_view(
    conn: psycopg.AsyncConnection,
    *,
    as_of: dt.date,
    scope: str,
    view_text: str,
    model: str,
    data_cutoff: dt.date,
    company_id: uuid.UUID | None = None,
    theme: str | None = None,
    used_fact_ids: list[uuid.UUID] | None = None,
) -> uuid.UUID:
    """Insert a free-text market view (immutable — never edited in place).

    Args:
        conn: An open connection.
        as_of: Market date the view refers to.
        scope: 'market' | 'ticker' | 'theme'. Not validated here — the
            caller (`agent/views.py`) validates scope/company_id/theme
            consistency; the database's `view_scope_target` CHECK
            constraint is the last line of defense.
        view_text: Free-text view content.
        model: Model identifier that produced the view (stored as
            `written_by`).
        data_cutoff: Date of the newest data used to write the view.
        company_id: The view's subject when scope == 'ticker'.
        theme: Theme label when scope == 'theme'.
        used_fact_ids: company_facts ids that informed this view
            (provenance). Not a DB-enforced foreign key — Postgres has no
            array foreign keys — just a recorded list.

    Returns:
        The new view_id.
    """
    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO market_views "
                "(as_of, scope, company_id, theme, view_text, written_by, data_cutoff, "
                "used_fact_ids) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING view_id",
                (as_of, scope, company_id, theme, view_text, model, data_cutoff, used_fact_ids),
            )
        ).fetchone()
    return row[0]


async def get_market_views(
    conn: psycopg.AsyncConnection,
    *,
    as_of: dt.date,
    company_id: uuid.UUID | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return market views as of a date (as_of <= :as_of), newest first.

    Args:
        conn: An open connection.
        as_of: Views referring to this date or earlier are returned.
        company_id: Optional filter, scope == 'ticker' rows for this company.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        limit: Maximum number of views to return.

    Returns:
        One dict per view: view_id, as_of, scope, company_id, theme,
        view_text, model, data_cutoff, used_fact_ids, created_at. Ordered
        by as_of descending, then created_at descending.
    """
    clauses = ["as_of <= %s"]
    params: list[object] = [as_of]
    if company_id is not None:
        clauses.append("company_id = %s")
        params.append(company_id)
    if scope is not None:
        clauses.append("scope = %s")
        params.append(scope)
    params.append(limit)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT view_id, as_of, scope, company_id, theme, view_text,
                   written_by AS model, data_cutoff, used_fact_ids, created_at
            FROM market_views
            WHERE {" AND ".join(clauses)}
            ORDER BY as_of DESC, created_at DESC
            LIMIT %s
            """,
            params,
        )
        return await cur.fetchall()


Extractor = Callable[[uuid.UUID, str, str], Awaitable[dict[str, object]]]


async def _default_extractor(view_id: uuid.UUID, view_text: str, model: str) -> dict[str, object]:
    """Placeholder extractor — LLM wiring is unfinished roadmap work."""
    raise NotImplementedError(
        f"LLM extractor not wired yet (view {view_id}); see agent/prompts/view_structuring.md"
    )


async def structure_views(
    conn: psycopg.AsyncConnection,
    *,
    extractor: Extractor | None = None,
    schema_version: int = 1,
    extraction_model: str = "claude-sonnet-4-5",
) -> int:
    """Structure free-text views with no row for this schema_version yet.

    Idempotent per (view_id, schema_version): a view already structured
    under a given schema_version is skipped for that version, but can still
    be (re-)structured under a different schema_version — the DuckDB
    version's bug (todo.md A1.9) of blocking all future re-structuring once
    any row existed is fixed by the (view_id, schema_version) primary key.

    Args:
        conn: An open connection.
        extractor: Async callable(view_id, view_text, model) ->
            JSON-serialisable dict. Defaults to a stub that raises
            NotImplementedError.
        schema_version: Schema version stored with each extraction.
        extraction_model: Model identifier recorded for provenance.

    Returns:
        Number of views structured in this run.

    Raises:
        NotImplementedError: If no extractor is supplied and the default is used.
    """
    extract = extractor or _default_extractor

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT mv.view_id, mv.view_text, mv.written_by AS model
            FROM market_views mv
            LEFT JOIN market_view_structures mvs
              ON mvs.view_id = mv.view_id AND mvs.schema_version = %s
            WHERE mvs.view_id IS NULL
            ORDER BY mv.created_at
            """,
            (schema_version,),
        )
        pending = await cur.fetchall()

    structured = 0
    for row in pending:
        payload = await extract(row["view_id"], row["view_text"], row["model"])
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO market_view_structures "
                "(view_id, schema_version, structure, extracted_by) "
                "VALUES (%s, %s, %s, %s)",
                (row["view_id"], schema_version, Jsonb(payload), extraction_model),
            )
        structured += 1

    return structured
