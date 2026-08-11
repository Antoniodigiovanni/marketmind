"""Company profile repository — point-in-time generated company profiles."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


async def create_profile(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    description: str,
    products_services: list[str],
    valid_from: dt.date,
    generated_by: str,
    website_url: str | None = None,
    sources: dict[str, object] | None = None,
    supersedes_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Write a new company profile in one transaction.

    Args:
        conn: An open connection.
        company_id: The profile's subject.
        description: Generated one-paragraph profile.
        products_services: Category strings.
        valid_from: Date the profile became valid.
        generated_by: Model id that produced the profile.
        website_url: Official website used as input, if any.
        sources: JSON-serialisable provenance dict. Stored as {} when None.
        supersedes_id: A prior profile this one replaces, if any. That row's
            valid_until is set to this profile's valid_from.

    Returns:
        The new profile_id.
    """
    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO company_profiles "
                "(company_id, description, products_services, website_url, sources, "
                "generated_by, valid_from, supersedes_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING profile_id",
                (
                    company_id,
                    description,
                    products_services,
                    website_url,
                    Jsonb(sources or {}),
                    generated_by,
                    valid_from,
                    supersedes_id,
                ),
            )
        ).fetchone()
        profile_id = row[0]

        if supersedes_id is not None:
            await conn.execute(
                "UPDATE company_profiles SET valid_until = %s "
                "WHERE profile_id = %s AND valid_until IS NULL",
                (valid_from, supersedes_id),
            )

    return profile_id


async def get_current_profile(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    as_of: dt.date,
) -> dict[str, object] | None:
    """Return the company profile valid as of a date, or None.

    Args:
        conn: An open connection.
        company_id: The company to fetch a profile for.
        as_of: The date to resolve as of.

    Returns:
        A dict with keys profile_id, company_id, description,
        products_services, website_url, sources, generated_by, valid_from,
        valid_until, or None if no profile is current as of the date.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT profile_id, company_id, description, products_services, "
            "website_url, sources, generated_by, valid_from, valid_until "
            "FROM company_profiles "
            "WHERE company_id = %s AND valid_from <= %s "
            "AND (valid_until IS NULL OR valid_until > %s) "
            "ORDER BY valid_from DESC LIMIT 1",
            (company_id, as_of, as_of),
        )
        return await cur.fetchone()
