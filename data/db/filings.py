"""SEC filings repository — filing metadata, full text, and fetch checkpoints.

Ownership (insider transactions/holdings) and 13F holdings are out of
scope here — they stay on the DuckDB store (`data/filings/store.py`)
until a follow-up cutover; see docs/superpowers/plans/
2026-08-11-postgres-filings-cutover.md.

`fetch_checkpoints` is a generic per-(source, key) resumability table
(also used, in principle, by other daily-fetch subsystems); the
functions here are scoped to what the filings pipeline needs today.
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.rows import dict_row


async def create_filing(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID | None,
    accession: str,
    form_type: str,
    filed_at: dt.date,
    period: dt.date | None,
    url: str,
    status: str = "fetched",
) -> uuid.UUID:
    """Insert a filing metadata row and return its id.

    Args:
        conn: An open connection.
        company_id: The filing's issuer, or None if it doesn't resolve to
            a tracked company (still worth storing).
        accession: SEC accession number; unique across all filings.
        form_type: e.g. '10-K', '8-K', '4', '13F-HR'.
        filed_at: Date the filing was submitted.
        period: Period of report, if the filing declares one.
        url: EDGAR archive URL for the primary document.
        status: Pipeline status; defaults to 'fetched'.

    Returns:
        The new filing_id.

    Raises:
        psycopg.errors.UniqueViolation: If accession already exists.
            Callers doing incremental fetches should check filing_exists
            first to keep re-fetches a no-op rather than an error.
    """
    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO filings (company_id, accession, form_type, filed_at, period, "
                "url, status) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING filing_id",
                (company_id, accession, form_type, filed_at, period, url, status),
            )
        ).fetchone()
        return row[0]


async def filing_exists(conn: psycopg.AsyncConnection, *, accession: str) -> bool:
    """Return True if a filing with this accession number is already stored.

    Args:
        conn: An open connection.
        accession: SEC accession number.

    Returns:
        True if present.
    """
    row = await (
        await conn.execute("SELECT 1 FROM filings WHERE accession = %s", (accession,))
    ).fetchone()
    return row is not None


async def insert_filing_text(
    conn: psycopg.AsyncConnection,
    *,
    filing_id: uuid.UUID,
    body: str,
    section: str | None = None,
) -> None:
    """Store normalized filing text for full-text search.

    Args:
        conn: An open connection.
        filing_id: The filing this text belongs to.
        body: Normalized plain text.
        section: Optional section label; None stores the whole document
            as a single blob, which is all the current pipeline produces.
    """
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO filing_text (filing_id, section, body) VALUES (%s, %s, %s)",
            (filing_id, section, body),
        )


async def get_filings(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    form_types: list[str] | None = None,
    filed_from: dt.date | None = None,
    filed_to: dt.date | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return filing metadata for a company.

    Args:
        conn: An open connection.
        company_id: The issuer to fetch filings for.
        form_types: Optional form types to include (e.g. ['10-K', '8-K']).
        filed_from: Optional earliest filing date.
        filed_to: Optional latest filing date.
        limit: Maximum rows.

    Returns:
        One dict per filing: filing_id, company_id, accession, form_type,
        filed_at, period, url, status, ordered by filed_at descending.
    """
    query = (
        "SELECT filing_id, company_id, accession, form_type, filed_at, period, url, status "
        "FROM filings WHERE company_id = %s"
    )
    params: list[object] = [company_id]
    if form_types:
        query += " AND form_type = ANY(%s)"
        params.append(form_types)
    if filed_from is not None:
        query += " AND filed_at >= %s"
        params.append(filed_from)
    if filed_to is not None:
        query += " AND filed_at <= %s"
        params.append(filed_to)
    query += " ORDER BY filed_at DESC LIMIT %s"
    params.append(limit)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def search_filing_text(
    conn: psycopg.AsyncConnection,
    *,
    query: str,
    company_id: uuid.UUID | None = None,
    form_types: list[str] | None = None,
    filed_from: dt.date | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Full-text search across stored filing text (all terms must match).

    Args:
        conn: An open connection.
        query: Free-text search terms, whitespace-separated.
        company_id: Optional issuer filter.
        form_types: Optional form types to include.
        filed_from: Optional earliest filing date.
        limit: Maximum rows.

    Returns:
        One dict per hit: filing_id, company_id, form_type, filed_at, url,
        snippet (240 chars centered on the first search term), ordered by
        filed_at descending. Empty list if query has no terms.
    """
    terms = [t for t in query.strip().lower().split() if t]
    if not terms:
        return []
    first = terms[0]

    sql = (
        "SELECT f.filing_id, f.company_id, f.form_type, f.filed_at, f.url, "
        "substr(t.body, greatest(strpos(lower(t.body), %s) - 120, 1), 240) AS snippet "
        "FROM filing_text t JOIN filings f ON f.filing_id = t.filing_id "
        "WHERE t.body ILIKE %s"
    )
    params: list[object] = [first, f"%{first}%"]
    for term in terms[1:]:
        sql += " AND t.body ILIKE %s"
        params.append(f"%{term}%")
    if company_id is not None:
        sql += " AND f.company_id = %s"
        params.append(company_id)
    if form_types:
        sql += " AND f.form_type = ANY(%s)"
        params.append(form_types)
    if filed_from is not None:
        sql += " AND f.filed_at >= %s"
        params.append(filed_from)
    sql += " ORDER BY f.filed_at DESC LIMIT %s"
    params.append(limit)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def has_checkpoint(conn: psycopg.AsyncConnection, *, source: str, key: str) -> bool:
    """Return True if a checkpoint exists for (source, key).

    Args:
        conn: An open connection.
        source: Checkpoint namespace, e.g. 'submissions:AAPL' or '13f'.
        key: Checkpoint key within that namespace.

    Returns:
        True if present.
    """
    row = await (
        await conn.execute(
            "SELECT 1 FROM fetch_checkpoints WHERE source = %s AND key = %s", (source, key)
        )
    ).fetchone()
    return row is not None


async def mark_checkpoint(
    conn: psycopg.AsyncConnection,
    *,
    source: str,
    key: str,
    last_date: dt.date | None = None,
) -> None:
    """Record that (source, key) has been processed.

    Args:
        conn: An open connection.
        source: Checkpoint namespace.
        key: Checkpoint key within that namespace.
        last_date: Optional bookkeeping date to store alongside the mark.
    """
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO fetch_checkpoints (source, key, last_date) VALUES (%s, %s, %s) "
            "ON CONFLICT (source, key) DO UPDATE SET "
            "last_date = EXCLUDED.last_date, updated_at = NOW()",
            (source, key, last_date),
        )
