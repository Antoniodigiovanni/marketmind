"""Company identity repository — the companies table only.

Ticker/ISIN/CUSIP resolution lives in data/db/identifiers.py; this module
owns just the entity row itself.
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.rows import dict_row


async def create_company(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    country: str | None,
    sector: str | None,
    first_seen: dt.date,
) -> uuid.UUID:
    """Insert a new company row.

    Args:
        conn: An open connection; scoped in its own transaction() block,
            nesting as a savepoint if one is already open on this connection.
        name: Display name.
        country: ISO country code, or None.
        sector: Free-form sector label, or None.
        first_seen: Date this company was first observed in any universe.

    Returns:
        The generated company_id.
    """
    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO companies (name, country, sector, first_seen) "
                "VALUES (%s, %s, %s, %s) RETURNING company_id",
                (name, country, sector, first_seen),
            )
        ).fetchone()
    return row[0]


async def update_company_attributes(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    name: str,
    country: str | None,
    sector: str | None,
) -> None:
    """Refresh name/country/sector on an existing company, bumping updated_at.

    Args:
        conn: An open connection.
        company_id: The company to update.
        name: New display name.
        country: New ISO country code, or None.
        sector: New sector label, or None.
    """
    async with conn.transaction():
        await conn.execute(
            "UPDATE companies SET name = %s, country = %s, sector = %s, updated_at = NOW() "
            "WHERE company_id = %s",
            (name, country, sector, company_id),
        )


async def update_company_description(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    description: str,
    description_source: str | None,
) -> None:
    """Overwrite a company's cached description, bumping updated_at.

    Unlike update_company_attributes, this always overwrites -- there is no
    NULL-preservation behavior here, matching the original DuckDB
    upsert_company's treatment of these two columns specifically.

    Args:
        conn: An open connection.
        company_id: The company to update.
        description: New description text.
        description_source: Provenance tag, e.g. 'wikipedia', or None.
    """
    async with conn.transaction():
        await conn.execute(
            "UPDATE companies SET description = %s, description_source = %s, updated_at = NOW() "
            "WHERE company_id = %s",
            (description, description_source, company_id),
        )


async def get_company(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
) -> dict[str, object] | None:
    """Fetch a company row by id.

    Args:
        conn: An open connection.
        company_id: The company to fetch.

    Returns:
        A dict with keys company_id, name, country, sector, description,
        description_source, first_seen, last_seen, or None if no such
        company exists.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT company_id, name, country, sector, description, description_source, "
            "first_seen, last_seen FROM companies WHERE company_id = %s",
            (company_id,),
        )
        return await cur.fetchone()
