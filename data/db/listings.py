"""Provider-symbol listings repository — tradable lines under a company."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg


async def get_current_listing(
    conn: psycopg.AsyncConnection,
    *,
    provider: str,
    symbol: str,
) -> dict[str, object] | None:
    """The live listing row for (provider, symbol), or None.

    Args:
        conn: An open connection.
        provider: Data provider, e.g. 'yahoo'.
        symbol: Provider symbol, e.g. 'ASML.AS'.

    Returns:
        {'listing_id', 'company_id', 'status'}, or None if no live listing
        exists for this (provider, symbol).
    """
    row = await (
        await conn.execute(
            "SELECT listing_id, company_id, status FROM listings "
            "WHERE provider = %s AND symbol = %s AND valid_until IS NULL",
            (provider, symbol),
        )
    ).fetchone()
    if row is None:
        return None
    return {"listing_id": row[0], "company_id": row[1], "status": row[2]}


async def upsert_listing(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    provider: str,
    symbol: str,
    status: str,
    valid_from: dt.date,
) -> uuid.UUID:
    """Create the live listing for (provider, symbol) under company_id, or
    refresh its status if one already exists under the same company.

    Args:
        conn: An open connection.
        company_id: The owning company. Must match any existing live listing
            for this (provider, symbol) -- see Raises.
        provider: Data provider, e.g. 'yahoo'.
        symbol: Provider symbol, e.g. 'ASML.AS'.
        status: 'valid', 'review', or 'unresolved'.
        valid_from: Effective date if a new listing is created.

    Returns:
        The listing_id, new or existing.

    Raises:
        ValueError: If a live listing for (provider, symbol) already exists
            under a different company_id. Provider symbols get reused (Yahoo
            recycles tickers faster than official registries); silently
            reassigning one would misattribute price history, so this
            surfaces the conflict instead of guessing.
    """
    existing = await get_current_listing(conn, provider=provider, symbol=symbol)
    if existing is not None:
        if existing["company_id"] != company_id:
            raise ValueError(
                f"listing {provider}:{symbol} is already live under company "
                f"{existing['company_id']}, not {company_id}"
            )
        async with conn.transaction():
            await conn.execute(
                "UPDATE listings SET status = %s WHERE listing_id = %s",
                (status, existing["listing_id"]),
            )
        return existing["listing_id"]

    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO listings (company_id, provider, symbol, status, valid_from) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING listing_id",
                (company_id, provider, symbol, status, valid_from),
            )
        ).fetchone()
    return row[0]
