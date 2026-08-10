"""Universe membership sync and point-in-time reads.

Ports data/universe/resolve.py's ticker-to-Yahoo-symbol resolution onto the
identity model (companies/company_identifiers/listings) and adds
universe_members, which loader.get_universe's CSV read has no equivalent
for. Not wired into any caller yet -- data/universe/resolve.py and
data/fetch.py keep using the CSV/DuckDB path until their own phase.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from data.db.identifiers import get_or_create_identifier
from data.db.listings import upsert_listing
from data.universe.loader import UNIVERSE_DIR, load_all_members
from data.universe.sources.ric import RIC_OVERRIDES, ric_variants
from data.universe.sources.yahoo import search_by_id, symbol_has_data


def _resolve_symbol(row: dict[str, object], *, search: object) -> dict[str, str] | None:
    """Resolve one member's Yahoo symbol, trying candidates then the company name."""
    ticker = str(row["ticker"])
    if ticker in RIC_OVERRIDES:
        return {"symbol": RIC_OVERRIDES[ticker], "name": str(row["name"])}
    for candidate in ric_variants(ticker):
        hit = search(candidate)
        if hit:
            return hit
    if row.get("name"):
        return search(str(row["name"]), name_hint=str(row["name"]))
    return None


async def _resolve_and_store_listing(
    conn: psycopg.AsyncConnection,
    row: dict[str, object],
    company_id: uuid.UUID,
    *,
    search: object,
    check: object,
) -> str:
    """Resolve row's Yahoo symbol and upsert its listing. Returns the status."""
    found = _resolve_symbol(row, search=search)
    if not found:
        status = "unresolved"
        symbol = str(row["ticker"])
    else:
        symbol = found["symbol"]
        status = "valid" if check(symbol) else "review"
    await upsert_listing(
        conn,
        company_id=company_id,
        provider="yahoo",
        symbol=symbol,
        status=status,
        valid_from=row["added_date"],
    )
    return status


async def sync_universe(
    conn: psycopg.AsyncConnection,
    *,
    universe_dir: Path = UNIVERSE_DIR,
    searcher: object | None = None,
    checker: object | None = None,
) -> dict[str, int]:
    """Sync every universe CSV row into the identity model.

    Every row (active and historically removed) gets a company and a
    universe_members row, so point-in-time reads work for past dates. Only
    currently active rows (removed_date IS NULL) get a Yahoo listing
    resolved -- there is no reason to validate tradability for a member
    that has already left the universe. Ticker is currently the only
    identifier resolved (neither universe CSV has isin/cusip columns yet,
    todo.md A1.8).

    Args:
        conn: An open connection.
        universe_dir: Directory containing the universe CSVs.
        searcher: Callable(id_value, name_hint=None) -> {'symbol','name'} or
            None; defaults to data.universe.sources.yahoo.search_by_id.
        checker: Callable(symbol) -> bool; defaults to symbol_has_data.

    Returns:
        Stats dict: {members, valid, review, unresolved}.
    """
    search = searcher if searcher is not None else search_by_id
    check = checker if checker is not None else symbol_has_data
    stats = {"members": 0, "valid": 0, "review": 0, "unresolved": 0}
    unresolved: list[tuple[dict[str, object], uuid.UUID]] = []

    for row in load_all_members(universe_dir).to_dicts():
        company_id = await get_or_create_identifier(
            conn,
            id_type="ticker",
            id_value=str(row["ticker"]),
            valid_from=row["added_date"],
            source="universe-csv",
            name=str(row["name"]),
            country=row.get("country"),
            sector=row.get("sector"),
        )

        async with conn.transaction():
            await conn.execute(
                "INSERT INTO universe_members (universe, company_id, added_date, removed_date) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (universe, company_id, added_date) DO UPDATE SET "
                "removed_date = excluded.removed_date",
                (row["universe"], company_id, row["added_date"], row.get("removed_date")),
            )
        stats["members"] += 1

        if row.get("removed_date") is not None:
            continue

        status = await _resolve_and_store_listing(conn, row, company_id, search=search, check=check)
        stats[status] += 1
        if status == "unresolved":
            unresolved.append((row, company_id))

    for row, company_id in unresolved:
        status = await _resolve_and_store_listing(conn, row, company_id, search=search, check=check)
        if status == "unresolved":
            continue
        stats[status] += 1
        stats["unresolved"] -= 1

    return stats


async def get_universe_members(
    conn: psycopg.AsyncConnection,
    *,
    universe: str,
    as_of: dt.date,
) -> list[dict[str, object]]:
    """Point-in-time constituents of `universe` as of a date.

    Args:
        conn: An open connection.
        universe: 'sp500' or 'eurostoxx600'.
        as_of: The date to resolve membership as of.

    Returns:
        One dict per member: company_id, name, country, sector, added_date,
        removed_date, yahoo_symbol (None if no Yahoo listing was live as of
        `as_of`).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT c.company_id, c.name, c.country, c.sector,
                   um.added_date, um.removed_date, l.symbol AS yahoo_symbol
            FROM universe_members um
            JOIN companies c ON c.company_id = um.company_id
            LEFT JOIN listings l
              ON l.company_id = um.company_id AND l.provider = 'yahoo'
                 AND l.valid_from <= %s AND (l.valid_until IS NULL OR l.valid_until > %s)
            WHERE um.universe = %s
              AND um.added_date <= %s
              AND (um.removed_date IS NULL OR um.removed_date > %s)
            """,
            (as_of, as_of, universe, as_of, as_of),
        )
        return await cur.fetchall()
