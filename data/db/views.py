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
    schema_version: int | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return market views as of a date (as_of <= :as_of), newest first.

    Args:
        conn: An open connection.
        as_of: Views referring to this date or earlier are returned.
        company_id: Optional filter, scope == 'ticker' rows for this company.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        schema_version: Optional derived-structure version. When omitted, the
            newest available version is returned for each view.
        limit: Maximum number of views to return.

    Returns:
        One dict per view: view_id, as_of, scope, company_id, theme,
        view_text, model, data_cutoff, used_fact_ids, created_at, structure,
        structure_schema_version, structured_by, and structured_at. Derived
        fields are None when the view has not been structured. Ordered by
        as_of descending, then created_at descending.

    Raises:
        ValueError: If schema_version is not positive.
    """
    if schema_version is not None and (isinstance(schema_version, bool) or schema_version < 1):
        raise ValueError("schema_version must be positive")

    clauses = ["mv.as_of <= %s"]
    params: list[object] = [as_of]
    if company_id is not None:
        clauses.append("mv.company_id = %s")
        params.append(company_id)
    if scope is not None:
        clauses.append("mv.scope = %s")
        params.append(scope)
    structure_version_clause = ""
    if schema_version is not None:
        structure_version_clause = "AND candidate.schema_version = %s"
        # The lateral subquery appears before the outer WHERE placeholders.
        params.insert(0, schema_version)
    params.append(limit)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT mv.view_id, mv.as_of, mv.scope, mv.company_id, mv.theme,
                   mv.view_text, mv.written_by AS model, mv.data_cutoff,
                   mv.used_fact_ids, mv.created_at,
                   derived.structure,
                   derived.schema_version AS structure_schema_version,
                   derived.extracted_by AS structured_by,
                   derived.created_at AS structured_at
            FROM market_views mv
            LEFT JOIN LATERAL (
                SELECT candidate.schema_version, candidate.structure,
                       candidate.extracted_by, candidate.created_at
                FROM market_view_structures candidate
                WHERE candidate.view_id = mv.view_id
                  {structure_version_clause}
                ORDER BY candidate.schema_version DESC
                LIMIT 1
            ) derived ON TRUE
            WHERE {" AND ".join(clauses)}
            ORDER BY mv.as_of DESC, mv.created_at DESC
            LIMIT %s
            """,
            params,
        )
        return await cur.fetchall()


Extractor = Callable[[dict[str, object]], Awaitable[dict[str, object]]]


async def structure_views(
    conn: psycopg.AsyncConnection,
    *,
    extractor: Extractor,
    extraction_model: str,
    schema_version: int = 1,
    as_of: dt.date | None = None,
    limit: int = 100,
) -> int:
    """Structure free-text views with no row for this schema_version yet.

    Idempotent per (view_id, schema_version): a view already structured
    under a given schema_version is skipped for that version, but can still
    be (re-)structured under a different schema_version — the DuckDB
    version's bug (todo.md A1.9) of blocking all future re-structuring once
    any row existed is fixed by the (view_id, schema_version) primary key.

    Args:
        conn: An open connection.
        extractor: Async callable(view row) -> JSON-serialisable dict. The row
            contains view_id, as_of, scope, company_id, theme, view_text, and
            the model that wrote the source view.
        schema_version: Schema version stored with each extraction.
        extraction_model: Model identifier recorded for provenance.
        as_of: Optional upper bound on the source view's market date.
        limit: Maximum pending views to structure in this run.

    Returns:
        Number of views structured in this run.

    Raises:
        ValueError: If extraction_model is blank or numeric bounds are invalid.
    """
    if not extraction_model.strip():
        raise ValueError("extraction_model must be nonblank")
    if isinstance(schema_version, bool) or schema_version < 1:
        raise ValueError("schema_version must be positive")
    if isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be positive")

    clauses = ["mvs.view_id IS NULL"]
    params: list[object] = [schema_version]
    if as_of is not None:
        clauses.append("mv.as_of <= %s")
        params.append(as_of)
    params.append(limit)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT mv.view_id, mv.as_of, mv.scope, mv.company_id, mv.theme,
                   mv.view_text, mv.written_by AS model
            FROM market_views mv
            LEFT JOIN market_view_structures mvs
              ON mvs.view_id = mv.view_id AND mvs.schema_version = %s
            WHERE {" AND ".join(clauses)}
            ORDER BY mv.created_at
            LIMIT %s
            """,
            params,
        )
        pending = await cur.fetchall()

    extracted: list[tuple[dict[str, object], dict[str, object]]] = []
    for row in pending:
        payload = await extractor(row)
        extracted.append((row, payload))

    structured = 0
    async with conn.transaction():
        for row, payload in extracted:
            result = await conn.execute(
                "INSERT INTO market_view_structures "
                "(view_id, schema_version, structure, extracted_by) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (view_id, schema_version) DO NOTHING",
                (row["view_id"], schema_version, Jsonb(payload), extraction_model),
            )
            structured += result.rowcount

    return structured
