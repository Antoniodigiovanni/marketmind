"""SEC CIK mapping stored in PostgreSQL company identifiers."""

from __future__ import annotations

import psycopg

from data.db.identifiers import get_current_company_id
from data.sources.sec import EdgardClient


async def cik_for_ticker(conn: psycopg.AsyncConnection, ticker: str) -> str | None:
    """Return the current SEC CIK for a ticker, or None if unmapped."""
    company_id = await get_current_company_id(conn, id_type="ticker", id_value=ticker)
    if company_id is None:
        return None
    row = await (
        await conn.execute(
            "SELECT id_value FROM company_identifiers WHERE company_id = %s "
            "AND id_type = 'cik' AND valid_until IS NULL ORDER BY valid_from DESC LIMIT 1",
            (company_id,),
        )
    ).fetchone()
    return str(row[0]) if row else None


async def ensure_cik_map(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    tickers: list[str],
) -> int:
    """Attach SEC CIK identifiers to tracked companies missing one."""
    missing = [ticker for ticker in tickers if await cik_for_ticker(conn, ticker) is None]
    if not missing:
        return 0
    all_tickers = client.load_company_tickers()
    count = 0
    for ticker in missing:
        info = all_tickers.get(ticker)
        company_id = await get_current_company_id(conn, id_type="ticker", id_value=ticker)
        if info is None or company_id is None:
            continue
        row = await (
            await conn.execute(
                "SELECT first_seen FROM companies WHERE company_id = %s", (company_id,)
            )
        ).fetchone()
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO company_identifiers "
                "(company_id, id_type, id_value, valid_from, is_primary, source) "
                "VALUES (%s, 'cik', %s, %s, FALSE, 'sec') "
                "ON CONFLICT (id_type, id_value) WHERE valid_until IS NULL DO NOTHING",
                (company_id, info["cik"], row[0]),
            )
        count += 1
    return count
