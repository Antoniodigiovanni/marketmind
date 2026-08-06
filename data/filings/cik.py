"""CIK mapping sync — EDGAR company_tickers.json into symbol_map."""

from __future__ import annotations

import duckdb

from data.sources.sec import EdgardClient

CIK_PROVIDER = "sec"


def cik_for_ticker(conn: duckdb.DuckDBPyConnection, ticker: str) -> str | None:
    """Return the 10-digit SEC CIK for a ticker, or None if unmapped."""
    row = conn.execute(
        """
        SELECT id_value FROM symbol_map
        WHERE id_type = 'CIK' AND provider = ? AND symbol = ?
        """,
        [CIK_PROVIDER, ticker],
    ).fetchone()
    return row[0] if row else None


def ensure_cik_map(
    conn: duckdb.DuckDBPyConnection,
    client: EdgardClient,
    tickers: list[str],
) -> int:
    """Upsert CIK rows into symbol_map for universe tickers missing one.

    Args:
        conn: DuckDB connection.
        client: EDGAR client used to fetch company_tickers.json.
        tickers: Tickers to resolve.

    Returns:
        Number of CIK rows inserted or updated.
    """
    missing = [t for t in tickers if cik_for_ticker(conn, t) is None]
    if not missing:
        return 0

    all_tickers = client.load_company_tickers()
    count = 0
    for ticker in missing:
        info = all_tickers.get(ticker)
        if info is None:
            continue
        conn.execute(
            """
            INSERT INTO symbol_map (id_type, id_value, provider, symbol, name, status)
            VALUES ('CIK', ?, ?, ?, ?, 'valid')
            ON CONFLICT (id_type, id_value, provider)
            DO UPDATE SET symbol = excluded.symbol, name = excluded.name, status = 'valid'
            """,
            [info["cik"], CIK_PROVIDER, ticker, info["name"]],
        )
        count += 1
    return count
