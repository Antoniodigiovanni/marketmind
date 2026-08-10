"""Identity resolution over company_identifiers — ticker/ISIN/CUSIP -> company_id.

Every resolution must be as-of dated; querying without a date is the same
class of error as reading facts without a point-in-time filter (CLAUDE.md).
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg

from data.db.companies import create_company, update_company_attributes


async def resolve_identifier(
    conn: psycopg.AsyncConnection,
    *,
    id_type: str,
    id_value: str,
    as_of: dt.date,
) -> uuid.UUID | None:
    """Resolve (id_type, id_value) to the company_id holding it on as_of.

    Args:
        conn: An open connection.
        id_type: One of 'ticker', 'isin', 'cusip', 'lei', 'cik', 'figi', 'ric'.
        id_value: The identifier value.
        as_of: The date to resolve as of.

    Returns:
        The company_id, or None if no identifier row covers that date.
    """
    row = await (
        await conn.execute(
            "SELECT company_id FROM company_identifiers "
            "WHERE id_type = %s AND id_value = %s "
            "AND valid_from <= %s AND (valid_until IS NULL OR valid_until > %s)",
            (id_type, id_value, as_of, as_of),
        )
    ).fetchone()
    return row[0] if row else None


async def get_current_company_id(
    conn: psycopg.AsyncConnection,
    *,
    id_type: str,
    id_value: str,
) -> uuid.UUID | None:
    """The company_id currently holding (id_type, id_value), or None.

    Args:
        conn: An open connection.
        id_type: One of 'ticker', 'isin', 'cusip', 'lei', 'cik', 'figi', 'ric'.
        id_value: The identifier value.

    Returns:
        The company_id, or None if no live identifier row exists.
    """
    row = await (
        await conn.execute(
            "SELECT company_id FROM company_identifiers "
            "WHERE id_type = %s AND id_value = %s AND valid_until IS NULL",
            (id_type, id_value),
        )
    ).fetchone()
    return row[0] if row else None


async def get_or_create_identifier(
    conn: psycopg.AsyncConnection,
    *,
    id_type: str,
    id_value: str,
    valid_from: dt.date,
    source: str,
    name: str,
    country: str | None,
    sector: str | None,
) -> uuid.UUID:
    """Return the company currently holding (id_type, id_value), creating one if needed.

    Does not detect or close out identifier reuse (a delisted ticker later
    reassigned to a different company) -- that is point-in-time universe
    backfill, tracked as separate follow-up work, not this function's job.
    This always defers to whatever is currently live for (id_type, id_value).

    Args:
        conn: An open connection.
        id_type: One of 'ticker', 'isin', 'cusip', 'lei', 'cik', 'figi', 'ric'.
        id_value: The identifier value.
        valid_from: Effective date if a new company/identifier is created.
        source: Provenance tag, e.g. 'universe-csv'.
        name: Display name -- refreshed on an existing match, used to create
            a new company otherwise.
        country: ISO country code, or None.
        sector: Sector label, or None.

    Returns:
        The resolved or newly created company_id.
    """
    existing = await get_current_company_id(conn, id_type=id_type, id_value=id_value)
    if existing is not None:
        await update_company_attributes(
            conn, company_id=existing, name=name, country=country, sector=sector
        )
        return existing

    async with conn.transaction():
        company_id = await create_company(
            conn, name=name, country=country, sector=sector, first_seen=valid_from
        )
        await conn.execute(
            "INSERT INTO company_identifiers "
            "(company_id, id_type, id_value, valid_from, is_primary, source) "
            "VALUES (%s, %s, %s, %s, TRUE, %s)",
            (company_id, id_type, id_value, valid_from, source),
        )
    return company_id
