"""PostgreSQL repository for SEC filings, ownership data, and checkpoints.

`fetch_checkpoints` is a generic per-(source, key) resumability table
(also used, in principle, by other daily-fetch subsystems); the
functions here are scoped to what the filings pipeline needs today.
"""

from __future__ import annotations

import datetime as dt
import uuid

import polars as pl
import psycopg
from psycopg.rows import dict_row

from data.filings.parse.ownership import InsiderHolding, InsiderTransaction


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


async def insert_insider_rows(
    conn: psycopg.AsyncConnection,
    *,
    filing_id: uuid.UUID,
    company_id: uuid.UUID,
    transactions: list[InsiderTransaction],
    holdings: list[InsiderHolding],
) -> tuple[int, int]:
    """Insert parsed ownership rows and return transaction/holding counts."""
    async with conn.transaction():
        if transactions:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO insider_transactions "
                    "(filing_id, company_id, insider_name, insider_title, security_title, "
                    "transaction_date, transaction_code, shares, price, shares_held_after, "
                    "direct_or_indirect, is_derivative) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            filing_id,
                            company_id,
                            tx.owner_name,
                            tx.owner_title,
                            tx.security_title,
                            dt.date.fromisoformat(tx.transaction_date),
                            tx.transaction_code,
                            tx.shares_transacted,
                            tx.price_per_share,
                            tx.shares_held_after,
                            tx.direct_or_indirect,
                            False,
                        )
                        for tx in transactions
                        if tx.transaction_date
                    ],
                )
        if holdings:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO insider_holdings "
                    "(filing_id, company_id, insider_name, insider_title, security_title, as_of, "
                    "shares, direct_or_indirect, form_type, is_derivative) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            filing_id,
                            company_id,
                            holding.owner_name,
                            holding.owner_title,
                            holding.security_title,
                            dt.date.fromisoformat(holding.ref_date),
                            holding.shares_held_after,
                            holding.direct_or_indirect,
                            holding.form_type,
                            False,
                        )
                        for holding in holdings
                        if holding.ref_date
                    ],
                )
    return (
        sum(tx.transaction_date is not None for tx in transactions),
        sum(holding.ref_date is not None for holding in holdings),
    )


async def insert_13f_holdings(
    conn: psycopg.AsyncConnection,
    *,
    filing_id: uuid.UUID,
    frame: pl.DataFrame,
) -> int:
    """Insert parsed 13F holdings and return the row count."""
    rows = frame.to_dicts()
    if not rows:
        return 0
    insert_rows = [row for row in rows if row["cusip"]]
    async with conn.transaction(), conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO form13f_holdings "
            "(filing_id, manager_cik, manager_name, cusip, report_period, issuer_name, "
            "class_title, shares, value_usd, put_call) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    filing_id,
                    row["filer_cik"],
                    row["filer_name"],
                    row["cusip"],
                    dt.date.fromisoformat(str(row["report_period"])),
                    row["issuer_name"],
                    row["class_title"],
                    row["shares"],
                    row["value_usd"],
                    row["put_call"],
                )
                for row in insert_rows
            ],
        )
    return len(insert_rows)


async def get_insider_transactions(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    since: dt.date | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return insider transactions for a company."""
    query = (
        "SELECT transaction_id AS id, insider_name AS owner_name, "
        "insider_title AS owner_title, security_title, transaction_date, transaction_code, "
        "shares AS shares_transacted, price AS price_per_share, shares_held_after, "
        "direct_or_indirect FROM insider_transactions WHERE company_id = %s"
    )
    params: list[object] = [company_id]
    if since is not None:
        query += " AND transaction_date >= %s"
        params.append(since)
    query += " ORDER BY transaction_date DESC LIMIT %s"
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def get_insider_holdings(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    as_of: dt.date | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return insider holding snapshots for a company."""
    query = (
        "SELECT holding_id AS id, insider_name AS owner_name, insider_title AS owner_title, "
        "security_title, as_of AS ref_date, shares AS shares_held_after, "
        "direct_or_indirect, form_type FROM insider_holdings WHERE company_id = %s"
    )
    params: list[object] = [company_id]
    if as_of is not None:
        query += " AND as_of <= %s"
        params.append(as_of)
    query += " ORDER BY as_of DESC LIMIT %s"
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def get_13f_holdings(
    conn: psycopg.AsyncConnection,
    *,
    cusip: str,
    as_of: dt.date | None = None,
    min_value: float | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return point-in-time 13F holdings for a CUSIP."""
    query = (
        "SELECT manager_name AS filer_name, manager_cik AS filer_cik, report_period, "
        "issuer_name, class_title, value_usd, shares, put_call "
        "FROM form13f_holdings WHERE cusip = %s"
    )
    params: list[object] = [cusip]
    if as_of is not None:
        query += " AND report_period <= %s"
        params.append(as_of)
    if min_value is not None:
        query += " AND value_usd >= %s"
        params.append(min_value)
    query += " ORDER BY report_period DESC, value_usd DESC LIMIT %s"
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


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


async def get_checkpoint_date(
    conn: psycopg.AsyncConnection, *, source: str, key: str
) -> dt.date | None:
    """Return the last completed date for a checkpoint, if present.

    Args:
        conn: An open connection.
        source: Checkpoint namespace.
        key: Checkpoint key within that namespace.

    Returns:
        The stored last_date, or None when the checkpoint is absent or has no date.
    """
    row = await (
        await conn.execute(
            "SELECT last_date FROM fetch_checkpoints WHERE source = %s AND key = %s",
            (source, key),
        )
    ).fetchone()
    return row[0] if row else None


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
