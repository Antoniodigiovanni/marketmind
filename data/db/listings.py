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


async def get_current_listing_for_company(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    provider: str,
) -> dict[str, object] | None:
    """The live listing row for company_id under provider, or None.

    Args:
        conn: An open connection.
        company_id: The owning company.
        provider: Data provider, e.g. 'yahoo'.

    Returns:
        {'listing_id', 'symbol', 'status', 'valid_from'}, or None if this
        company has no live listing under this provider.
    """
    row = await (
        await conn.execute(
            "SELECT listing_id, symbol, status, valid_from FROM listings "
            "WHERE company_id = %s AND provider = %s AND valid_until IS NULL",
            (company_id, provider),
        )
    ).fetchone()
    if row is None:
        return None
    return {"listing_id": row[0], "symbol": row[1], "status": row[2], "valid_from": row[3]}


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
    refresh it if this company already has a live listing under this provider.

    A company has at most one live listing per provider. If the company's
    current listing under `provider` already has this symbol, only its
    status is refreshed (e.g. an 'unresolved' placeholder later confirmed
    'valid'). If it has a DIFFERENT symbol -- resolution corrected on a later
    sync, or a genuine re-listing -- the old row is retired (`valid_until`
    set) and a new one opened, so at most one listing per company/provider is
    ever live; a symbol resolved once and corrected the same day is dropped
    outright rather than retired, since a zero-duration row has no historical
    value and `valid_until` must be strictly after `valid_from`.

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
    by_symbol = await get_current_listing(conn, provider=provider, symbol=symbol)
    if by_symbol is not None and by_symbol["company_id"] != company_id:
        raise ValueError(
            f"listing {provider}:{symbol} is already live under company "
            f"{by_symbol['company_id']}, not {company_id}"
        )

    current = await get_current_listing_for_company(conn, company_id=company_id, provider=provider)

    async with conn.transaction():
        if current is not None and current["symbol"] == symbol:
            await conn.execute(
                "UPDATE listings SET status = %s WHERE listing_id = %s",
                (status, current["listing_id"]),
            )
            return current["listing_id"]

        if current is not None:
            if current["valid_from"] == valid_from:
                await conn.execute(
                    "DELETE FROM listings WHERE listing_id = %s", (current["listing_id"],)
                )
            else:
                await conn.execute(
                    "UPDATE listings SET valid_until = %s WHERE listing_id = %s",
                    (valid_from, current["listing_id"]),
                )

        row = await (
            await conn.execute(
                "INSERT INTO listings (company_id, provider, symbol, status, valid_from) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING listing_id",
                (company_id, provider, symbol, status, valid_from),
            )
        ).fetchone()
    return row[0]
