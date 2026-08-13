"""Prices repository — OHLCV bars, FX conversion, and derived volatility.

Every row is keyed on `listing_id`, not `company_id` — currency and
exchange calendar are properties of the listing (see schema_postgres.sql's
`prices` table comment), and a company can have more than one listing.
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.rows import dict_row

UPSERT_CHUNK_SIZE = 500


async def upsert_prices(
    conn: psycopg.AsyncConnection,
    *,
    listing_id: uuid.UUID,
    rows: list[dict[str, object]],
) -> int:
    """Insert or update OHLCV bars for a listing.

    Args:
        conn: An open connection.
        listing_id: The listing these bars belong to.
        rows: Each dict has 'date', 'open', 'high', 'low', 'close',
            'volume', 'currency' (ISO 4217). 'open'/'high'/'low'/'volume'
            may be None; 'close' and 'currency' are required.

    Returns:
        Number of rows written (inserted or updated).
    """
    if not rows:
        return 0

    async with conn.transaction():
        for start in range(0, len(rows), UPSERT_CHUNK_SIZE):
            chunk = rows[start : start + UPSERT_CHUNK_SIZE]
            placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(chunk))
            params: list[object] = []
            for row in chunk:
                params.extend(
                    [
                        listing_id,
                        row["date"],
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row["close"],
                        row.get("volume"),
                        row["currency"],
                    ]
                )
            await conn.execute(
                "INSERT INTO prices (listing_id, date, open, high, low, close, volume, currency) "
                f"VALUES {placeholders} "
                "ON CONFLICT (listing_id, date) DO UPDATE SET "
                "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
                "close = EXCLUDED.close, volume = EXCLUDED.volume, currency = EXCLUDED.currency",
                params,
            )
    return len(rows)


async def get_last_price_date(
    conn: psycopg.AsyncConnection,
    *,
    listing_id: uuid.UUID,
) -> dt.date | None:
    """Return the latest stored bar date for a listing, or None.

    Args:
        conn: An open connection.
        listing_id: The listing to check.

    Returns:
        The max date in `prices` for this listing, or None if it has no
        bars yet.
    """
    row = await (
        await conn.execute("SELECT MAX(date) FROM prices WHERE listing_id = %s", (listing_id,))
    ).fetchone()
    return row[0] if row else None


async def get_prices(
    conn: psycopg.AsyncConnection,
    *,
    listing_id: uuid.UUID,
    start: dt.date | None = None,
    end: dt.date | None = None,
    to_currency: str | None = None,
) -> list[dict[str, object]]:
    """Return OHLCV bars for a listing, optionally converted to a currency.

    Args:
        conn: An open connection.
        listing_id: The listing to read.
        start: Optional earliest date (inclusive).
        end: Optional latest date (inclusive).
        to_currency: Optional ISO 4217 code. When set and different from a
            bar's stored currency, open/high/low/close are converted using
            the most recent ECB reference rate on or before that bar's
            date (see `upsert_fx_rates`) -- never an exact-date join, since
            FX holidays don't align with equity calendars. A bar for which
            no rate is available on or before its date converts to NULL
            prices rather than silently falling back to the original
            currency.

    Returns:
        Rows ordered by date ascending: {date, open, high, low, close, volume}.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        if to_currency is None:
            await cur.execute(
                "SELECT date, open, high, low, close, volume FROM prices "
                "WHERE listing_id = %s "
                "AND (%s::date IS NULL OR date >= %s) "
                "AND (%s::date IS NULL OR date <= %s) "
                "ORDER BY date",
                (listing_id, start, start, end, end),
            )
            return await cur.fetchall()

        await cur.execute(
            """
            SELECT p.date,
                   p.open  * fx.factor AS open,
                   p.high  * fx.factor AS high,
                   p.low   * fx.factor AS low,
                   p.close * fx.factor AS close,
                   p.volume
            FROM prices p
            CROSS JOIN LATERAL (
                SELECT
                    CASE WHEN p.currency = %(to_ccy)s THEN 1.0 ELSE
                        (CASE WHEN %(to_ccy)s = 'EUR' THEN 1.0 ELSE (
                            SELECT r.rate FROM fx_rates r
                            WHERE r.base_currency = 'EUR' AND r.quote_currency = %(to_ccy)s
                              AND r.date <= p.date ORDER BY r.date DESC LIMIT 1
                        ) END)
                        /
                        (CASE WHEN p.currency = 'EUR' THEN 1.0 ELSE (
                            SELECT r.rate FROM fx_rates r
                            WHERE r.base_currency = 'EUR' AND r.quote_currency = p.currency
                              AND r.date <= p.date ORDER BY r.date DESC LIMIT 1
                        ) END)
                    END AS factor
            ) fx
            WHERE p.listing_id = %(listing_id)s
              AND (%(start)s::date IS NULL OR p.date >= %(start)s)
              AND (%(end)s::date IS NULL OR p.date <= %(end)s)
            ORDER BY p.date
            """,
            {
                "to_ccy": to_currency,
                "listing_id": listing_id,
                "start": start,
                "end": end,
            },
        )
        return await cur.fetchall()


async def upsert_fx_rates(
    conn: psycopg.AsyncConnection,
    *,
    base_currency: str,
    rows: list[dict[str, object]],
) -> int:
    """Insert or update daily FX reference rates.

    Args:
        conn: An open connection.
        base_currency: The base currency all rows share, e.g. 'EUR'.
        rows: Each dict has 'quote_currency', 'date', 'rate' (> 0),
            'source'.

    Returns:
        Number of rows written (inserted or updated).
    """
    if not rows:
        return 0

    async with conn.transaction():
        for start in range(0, len(rows), UPSERT_CHUNK_SIZE):
            chunk = rows[start : start + UPSERT_CHUNK_SIZE]
            placeholders = ", ".join(["(%s, %s, %s, %s, %s)"] * len(chunk))
            params: list[object] = []
            for row in chunk:
                params.extend(
                    [row["date"], base_currency, row["quote_currency"], row["rate"], row["source"]]
                )
            await conn.execute(
                "INSERT INTO fx_rates (date, base_currency, quote_currency, rate, source) "
                f"VALUES {placeholders} "
                "ON CONFLICT (date, base_currency, quote_currency) DO UPDATE SET "
                "rate = EXCLUDED.rate, source = EXCLUDED.source",
                params,
            )
    return len(rows)


async def get_last_fx_date(
    conn: psycopg.AsyncConnection,
    *,
    base_currency: str,
    quote_currency: str,
) -> dt.date | None:
    """Return the latest stored FX rate date for a currency pair, or None.

    Args:
        conn: An open connection.
        base_currency: e.g. 'EUR'.
        quote_currency: e.g. 'USD'.

    Returns:
        The max date in `fx_rates` for this pair, or None if absent.
    """
    row = await (
        await conn.execute(
            "SELECT MAX(date) FROM fx_rates WHERE base_currency = %s AND quote_currency = %s",
            (base_currency, quote_currency),
        )
    ).fetchone()
    return row[0] if row else None


async def replace_all_volatility(
    conn: psycopg.AsyncConnection,
    *,
    rows: list[dict[str, object]],
) -> int:
    """Recompute the whole `volatility` table (truncate, then bulk insert).

    Derived data, never appended to -- each run replaces the prior one in
    full, matching the schema's "truncate-and-rebuild" comment. An empty
    `rows` still clears the table (a run with no listings priced yet
    should leave no stale volatility behind).

    Args:
        conn: An open connection.
        rows: Each dict has 'listing_id', 'date', 'rv_20d', 'rv_60d'
            (the last two may be None, e.g. inside a rolling window's
            warm-up period).

    Returns:
        Number of rows written.
    """
    async with conn.transaction():
        await conn.execute("TRUNCATE volatility")
        for start in range(0, len(rows), UPSERT_CHUNK_SIZE):
            chunk = rows[start : start + UPSERT_CHUNK_SIZE]
            if not chunk:
                continue
            placeholders = ", ".join(["(%s, %s, %s, %s)"] * len(chunk))
            params: list[object] = []
            for row in chunk:
                params.extend([row["listing_id"], row["date"], row["rv_20d"], row["rv_60d"]])
            await conn.execute(
                f"INSERT INTO volatility (listing_id, date, rv_20d, rv_60d) VALUES {placeholders}",
                params,
            )
    return len(rows)


async def get_volatility(
    conn: psycopg.AsyncConnection,
    *,
    listing_id: uuid.UUID,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> list[dict[str, object]]:
    """Return realized-volatility rows for a listing.

    Args:
        conn: An open connection.
        listing_id: The listing to read.
        start: Optional earliest date (inclusive).
        end: Optional latest date (inclusive).

    Returns:
        Rows ordered by date ascending: {date, rv_20d, rv_60d}.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT date, rv_20d, rv_60d FROM volatility WHERE listing_id = %s "
            "AND (%s::date IS NULL OR date >= %s) "
            "AND (%s::date IS NULL OR date <= %s) "
            "ORDER BY date",
            (listing_id, start, start, end, end),
        )
        return await cur.fetchall()
