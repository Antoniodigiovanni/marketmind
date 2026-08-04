"""MCP server — exposes the alpha-wire market database to the agent harness."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
from mcp.server.fastmcp import FastMCP

from agent.knowledge import get_current_facts, write_fact
from agent.views import get_market_views as get_views
from agent.views import save_market_view as save_view

mcp = FastMCP("alpha-wire")

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "db" / "research.duckdb"
PRICES_DIR = ROOT_DIR / "data" / "raw" / "prices"
MACRO_DIR = ROOT_DIR / "data" / "raw" / "macro"
NEWS_GLOB = str(ROOT_DIR / "data" / "raw" / "news" / "*.ndjson")
PROCESSED_VOL_PATH = ROOT_DIR / "data" / "processed" / "volatility.parquet"


def _connect() -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection, creating nothing.

    Raises:
        FileNotFoundError: If research.duckdb has not been initialised.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not initialised: {DB_PATH}. Run `uv run python -m data.db.init` first."
        )
    return duckdb.connect(str(DB_PATH))


def _rows_to_dicts(rows: list[tuple[object, ...]], columns: list[str]) -> list[dict[str, object]]:
    """Convert DuckDB rows + column names into JSON-friendly dicts."""
    result: list[dict[str, object]] = []
    for row in rows:
        record: dict[str, object] = {}
        for column, value in zip(columns, row, strict=True):
            if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
                record[column] = value.isoformat()
            else:
                record[column] = value
        result.append(record)
    return result


@mcp.tool()
def get_prices(
    ticker: str, start: str | None = None, end: str | None = None
) -> list[dict[str, object]]:
    """Return daily OHLCV rows for a ticker.

    Args:
        ticker: Yahoo symbol, e.g. 'AAPL' or 'ASML.AS'.
        start: Optional start date (YYYY-MM-DD).
        end: Optional end date (YYYY-MM-DD).

    Returns:
        List of {date, open, high, low, close, volume}.
    """
    price_file = PRICES_DIR / f"{ticker}.parquet"
    if not price_file.exists():
        return []

    conn = duckdb.connect()
    try:
        query = "SELECT date, open, high, low, close, volume FROM read_parquet(?)"
        params: list[object] = [str(price_file)]
        if start or end:
            query += " WHERE date BETWEEN ? AND ?"
            params += [start or "1900-01-01", end or "2999-12-31"]
        query += " ORDER BY date"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, ["date", "open", "high", "low", "close", "volume"])


@mcp.tool()
def get_volatility(
    ticker: str, start: str | None = None, end: str | None = None
) -> list[dict[str, object]]:
    """Return volatility data for a ticker or market vol index.

    Args:
        ticker: Yahoo symbol ('AAPL', 'ASML.AS') or a vol index ('^VIX', '^VSTOXX').
        start: Optional start date (YYYY-MM-DD).
        end: Optional end date (YYYY-MM-DD).

    Returns:
        For a stock: {date, ticker, rv_20d, rv_60d} (annualised %).
        For a vol index: {date, ticker, close} from the macro price file.
    """
    if ticker in ("^VIX", "^VSTOXX"):
        return get_prices(ticker, start, end)

    if not PROCESSED_VOL_PATH.exists():
        return []
    conn = duckdb.connect()
    try:
        query = "SELECT date, ticker, rv_20d, rv_60d FROM read_parquet(?) WHERE ticker = ?"
        params: list[object] = [str(PROCESSED_VOL_PATH), ticker]
        if start or end:
            query += " AND date BETWEEN ? AND ?"
            params += [start or "1900-01-01", end or "2999-12-31"]
        query += " ORDER BY date"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, ["date", "ticker", "rv_20d", "rv_60d"])


@mcp.tool()
def get_news(ticker: str, since: str | None = None, limit: int = 50) -> list[dict[str, object]]:
    """Return news articles for a ticker from the raw ingest files.

    Args:
        ticker: Ticker symbol.
        since: Optional date filter (YYYY-MM-DD).
        limit: Maximum articles to return.

    Returns:
        List of {url, source, ticker, published_at, title, description}.
    """
    import glob

    files = glob.glob(NEWS_GLOB)
    if not files:
        return []

    conn = duckdb.connect()
    try:
        query = """
            SELECT url, source, ticker, published_at, title, description
            FROM read_ndjson(?, union_by_name = true)
            WHERE ticker = ?
        """
        params: list[object] = [files, ticker]
        if since:
            query += " AND published_at >= ?"
            params.append(since)
        query += " ORDER BY published_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, ["url", "source", "ticker", "published_at", "title", "description"])


@mcp.tool()
def search_news(query: str, limit: int = 50) -> list[dict[str, object]]:
    """Full-text search across ingested news articles.

    Args:
        query: Free-text search terms.
        limit: Maximum articles to return.

    Returns:
        List of {url, source, ticker, published_at, title, description} ranked
        by recency.
    """
    import glob

    files = glob.glob(NEWS_GLOB)
    if not files:
        return []

    conn = duckdb.connect()
    try:
        rows = conn.execute(
            """
            SELECT url, source, ticker, published_at, title, description
            FROM read_ndjson(?, union_by_name = true)
            WHERE title ILIKE ? OR description ILIKE ? OR content ILIKE ?
            ORDER BY published_at DESC LIMIT ?
            """,
            [files, f"%{query}%", f"%{query}%", f"%{query}%", limit],
        ).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, ["url", "source", "ticker", "published_at", "title", "description"])


@mcp.tool()
def get_facts(ticker: str, as_of: str) -> list[dict[str, object]]:
    """Return facts for a ticker as of a date (point-in-time safe).

    Args:
        ticker: Ticker symbol.
        as_of: Date string (YYYY-MM-DD) — required, never omitted.

    Returns:
        List of {id, fact_type, statement, confidence, valid_from, valid_until,
        source_article}.
    """
    with _connect() as conn:
        return get_current_facts(ticker, as_of, conn)


@mcp.tool()
def get_market_views(
    as_of: str,
    ticker: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return historical market views as of a date.

    Args:
        as_of: Date (YYYY-MM-DD); returns views for this date or earlier.
        ticker: Optional ticker filter.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        limit: Maximum views to return.

    Returns:
        List of view dicts ordered by as_of descending.
    """
    with _connect() as conn:
        return get_views(conn, as_of=as_of, ticker=ticker, scope=scope, limit=limit)


@mcp.tool()
def save_market_view_tool(
    as_of: str,
    view_text: str,
    model: str,
    scope: str = "market",
    ticker: str | None = None,
    theme: str | None = None,
    data_cutoff: str | None = None,
) -> str:
    """Persist a free-text market view (immutable once written).

    Args:
        as_of: Market date (YYYY-MM-DD) the view refers to.
        view_text: Free-text view content.
        model: Model identifier that produced the view.
        scope: 'market' | 'ticker' | 'theme'.
        ticker: Required when scope == 'ticker'.
        theme: Required when scope == 'theme'.
        data_cutoff: ISO timestamp of newest data used, optional.

    Returns:
        New view ID.
    """
    with _connect() as conn:
        return save_view(
            conn,
            as_of=as_of,
            view_text=view_text,
            model=model,
            scope=scope,
            ticker=ticker,
            theme=theme,
            data_cutoff=data_cutoff,
        )


@mcp.tool()
def save_fact(
    ticker: str,
    statement: str,
    fact_type: str,
    confidence: float,
    valid_from: str,
    source_article_id: str,
    supersedes_id: str | None = None,
) -> str:
    """Persist a versioned fact for a ticker.

    Args:
        ticker: Ticker symbol.
        statement: One-sentence fact statement.
        fact_type: Free-form category.
        confidence: 0.0–1.0.
        valid_from: Date the fact became valid (YYYY-MM-DD).
        source_article_id: FK to articles.id.
        supersedes_id: Optional ID of the fact this replaces.

    Returns:
        New fact ID.
    """
    with _connect() as conn:
        return write_fact(
            conn,
            ticker=ticker,
            statement=statement,
            fact_type=fact_type,
            confidence=confidence,
            valid_from=valid_from,
            source_article_id=source_article_id,
            supersedes_id=supersedes_id,
        )


@mcp.tool()
def run_ingest(date: str) -> str:
    """Trigger the daily news + fact ingest pipeline for a date.

    Args:
        date: Date string (YYYY-MM-DD) to ingest.

    Returns:
        Confirmation message once the pipeline completes.

    Raises:
        NotImplementedError: Until the pipeline lands (roadmap 1.4–1.5).
    """
    from agent.ingestion import run_daily_ingest

    run_daily_ingest(date)
    return f"Ingest completed for {date}"


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
