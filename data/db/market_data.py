"""PostgreSQL repositories for macro, dividends, and short interest."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.rows import dict_row


async def upsert_macro_series(
    conn: psycopg.AsyncConnection,
    *,
    series_id: str,
    source: str,
    title: str,
    unit: str,
    frequency: str,
    rows: list[dict[str, object]],
) -> int:
    """Upsert macro metadata and observations, returning rows supplied."""
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO macro_series (series_id, source, title, unit, frequency) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (series_id) DO UPDATE SET "
            "source = excluded.source, title = excluded.title, unit = excluded.unit, "
            "frequency = excluded.frequency",
            (series_id, source, title, unit, frequency),
        )
        if rows:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO macro_observations (series_id, date, value) "
                    "VALUES (%s, %s, %s) ON CONFLICT (series_id, date) "
                    "DO UPDATE SET value = excluded.value",
                    [(series_id, row["date"], row["value"]) for row in rows],
                )
    return len(rows)


async def get_macro(
    conn: psycopg.AsyncConnection,
    *,
    series_id: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> list[dict[str, object]]:
    """Return macro observations with their series metadata."""
    query = (
        "SELECT s.series_id AS series, s.title AS name, s.unit, s.frequency, o.date, o.value "
        "FROM macro_series s JOIN macro_observations o USING (series_id) WHERE s.series_id = %s"
    )
    params: list[object] = [series_id]
    if start is not None:
        query += " AND o.date >= %s"
        params.append(start)
    if end is not None:
        query += " AND o.date <= %s"
        params.append(end)
    query += " ORDER BY o.date"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def upsert_dividends(
    conn: psycopg.AsyncConnection,
    *,
    listing_id: uuid.UUID,
    rows: list[dict[str, object]],
) -> int:
    """Upsert dividend observations and return rows supplied."""
    if not rows:
        return 0
    async with conn.transaction(), conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO dividends (listing_id, ex_date, amount, currency) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (listing_id, ex_date) DO UPDATE SET "
            "amount = excluded.amount, currency = excluded.currency",
            [(listing_id, row["ex_date"], row["amount"], row["currency"]) for row in rows],
        )
    return len(rows)


async def get_last_dividend_date(
    conn: psycopg.AsyncConnection, *, listing_id: uuid.UUID
) -> dt.date | None:
    """Return the latest stored ex-date for a listing."""
    row = await (
        await conn.execute(
            "SELECT MAX(ex_date) FROM dividends WHERE listing_id = %s", (listing_id,)
        )
    ).fetchone()
    return row[0] if row else None


async def get_dividends(
    conn: psycopg.AsyncConnection,
    *,
    listing_id: uuid.UUID,
    since: dt.date | None = None,
    as_of: dt.date | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return dividend observations newest first."""
    query = "SELECT ex_date, amount, currency FROM dividends WHERE listing_id = %s"
    params: list[object] = [listing_id]
    if since is not None:
        query += " AND ex_date >= %s"
        params.append(since)
    if as_of is not None:
        query += " AND ex_date <= %s"
        params.append(as_of)
    query += " ORDER BY ex_date DESC LIMIT %s"
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def upsert_short_interest(
    conn: psycopg.AsyncConnection,
    *,
    listing_id: uuid.UUID,
    rows: list[dict[str, object]],
) -> int:
    """Upsert FINRA short-interest observations and return rows supplied."""
    if not rows:
        return 0
    async with conn.transaction(), conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO short_interest "
            "(listing_id, settlement_date, short_volume, total_volume, days_to_cover, "
            "issue_name, market_class, previous_short, change_pct) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (listing_id, settlement_date) DO UPDATE SET "
            "short_volume = excluded.short_volume, total_volume = excluded.total_volume, "
            "days_to_cover = excluded.days_to_cover, issue_name = excluded.issue_name, "
            "market_class = excluded.market_class, previous_short = excluded.previous_short, "
            "change_pct = excluded.change_pct",
            [
                (
                    listing_id,
                    row["settlement_date"],
                    row["current_short"],
                    row["avg_daily_volume"],
                    row["days_to_cover"],
                    row["issue_name"],
                    row["market_class"],
                    row["previous_short"],
                    row["change_pct"],
                )
                for row in rows
            ],
        )
    return len(rows)


async def get_short_interest(
    conn: psycopg.AsyncConnection,
    *,
    listing_id: uuid.UUID,
    as_of: dt.date | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return FINRA short-interest observations newest first."""
    query = (
        "SELECT issue_name, market_class, short_volume AS current_short, "
        "previous_short, total_volume AS avg_daily_volume, days_to_cover, change_pct, "
        "settlement_date FROM short_interest WHERE listing_id = %s"
    )
    params: list[object] = [listing_id]
    if as_of is not None:
        query += " AND settlement_date <= %s"
        params.append(as_of)
    query += " ORDER BY settlement_date DESC LIMIT %s"
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()
