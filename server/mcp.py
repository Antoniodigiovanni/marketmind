"""MCP server — exposes the marketmind market database to the agent harness."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import duckdb
from mcp.server.fastmcp import FastMCP

from agent.knowledge import get_current_facts, write_fact
from agent.views import get_market_views as get_views
from agent.views import save_market_view as save_view

mcp = FastMCP("marketmind")

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "db" / "research.duckdb"
PRICES_DIR = ROOT_DIR / "data" / "raw" / "prices"
MACRO_DIR = ROOT_DIR / "data" / "raw" / "macro"
DIVIDENDS_DIR = ROOT_DIR / "data" / "raw" / "dividends"
SHORT_INTEREST_GLOB = str(ROOT_DIR / "data" / "raw" / "short_interest" / "*.parquet")
NEWS_GLOB = str(ROOT_DIR / "data" / "raw" / "news" / "*.ndjson")
PROCESSED_VOL_PATH = ROOT_DIR / "data" / "processed" / "volatility.parquet"
FILINGS_13F_GLOB = str(ROOT_DIR / "data" / "raw" / "13f" / "**" / "*.parquet")


def _13f_partitions_exist() -> bool:
    """Return True if any 13F Parquet partition files exist."""
    import glob

    return bool(glob.glob(FILINGS_13F_GLOB))


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
def get_filings(
    ticker: str,
    form_types: list[str] | None = None,
    filed_from: str | None = None,
    filed_to: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return SEC filing metadata for a ticker.

    Args:
        ticker: Ticker symbol.
        form_types: Optional form types to include (e.g. ['10-K', '8-K']).
        filed_from: Optional earliest filing date (YYYY-MM-DD).
        filed_to: Optional latest filing date (YYYY-MM-DD).
        limit: Maximum rows.

    Returns:
        List of {id, accession_number, cik, ticker, form_type, filed_at,
        period_of_report, url, status, article_id} ordered by filed_at desc.
    """
    with _connect() as conn:
        query = """
            SELECT id, accession_number, cik, ticker, form_type, filed_at,
                   period_of_report, url, status, article_id
            FROM filings
            WHERE ticker = ?
        """
        params: list[object] = [ticker]
        if form_types:
            placeholders = ",".join("?" * len(form_types))
            query += f" AND form_type IN ({placeholders})"
            params.extend(form_types)
        if filed_from:
            query += " AND filed_at >= ?"
            params.append(filed_from)
        if filed_to:
            query += " AND filed_at <= ?"
            params.append(filed_to)
        query += " ORDER BY filed_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return _rows_to_dicts(
        rows,
        [
            "id",
            "accession_number",
            "cik",
            "ticker",
            "form_type",
            "filed_at",
            "period_of_report",
            "url",
            "status",
            "article_id",
        ],
    )


@mcp.tool()
def search_filings(
    query: str,
    ticker: str | None = None,
    form_types: list[str] | None = None,
    filed_from: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Full-text search across stored SEC filing text.

    Args:
        query: Free-text search terms (all terms must match).
        ticker: Optional ticker filter.
        form_types: Optional form types to include.
        filed_from: Optional earliest filing date (YYYY-MM-DD).
        limit: Maximum rows.

    Returns:
        List of {filing_id, ticker, form_type, filed_at, url, snippet} ordered
        by filed_at desc.
    """
    terms = [t for t in re.split(r"\s+", query.strip().lower()) if t]
    if not terms:
        return []
    first = terms[0]
    with _connect() as conn:
        sql = """
            SELECT f.id, f.ticker, f.form_type, f.filed_at, f.url,
                   substr(t.text, greatest(strpos(lower(t.text), ?) - 120, 1), 240) AS snippet
            FROM filing_text t
            JOIN filings f ON f.id = t.filing_id
            WHERE t.text ILIKE ?
        """
        params: list[object] = [first, f"%{first}%"]
        for term in terms[1:]:
            sql += " AND t.text ILIKE ?"
            params.append(f"%{term}%")
        if ticker:
            sql += " AND f.ticker = ?"
            params.append(ticker)
        if form_types:
            placeholders = ",".join("?" * len(form_types))
            sql += f" AND f.form_type IN ({placeholders})"
            params.extend(form_types)
        if filed_from:
            sql += " AND f.filed_at >= ?"
            params.append(filed_from)
        sql += " ORDER BY f.filed_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    return _rows_to_dicts(rows, ["filing_id", "ticker", "form_type", "filed_at", "url", "snippet"])


@mcp.tool()
def get_insider_transactions(
    ticker: str, since: str | None = None, limit: int = 100
) -> list[dict[str, object]]:
    """Return insider transaction rows (Form 4/5) for a ticker.

    Args:
        ticker: Ticker symbol.
        since: Optional earliest transaction date (YYYY-MM-DD).
        limit: Maximum rows.

    Returns:
        List of {id, owner_name, owner_title, security_title, transaction_date,
        transaction_code, shares_transacted, price_per_share, shares_held_after,
        direct_or_indirect} ordered by transaction_date desc.
    """
    with _connect() as conn:
        query = """
            SELECT id, owner_name, owner_title, security_title, transaction_date,
                   transaction_code, shares_transacted, price_per_share,
                   shares_held_after, direct_or_indirect
            FROM insider_transactions
            WHERE ticker = ?
        """
        params: list[object] = [ticker]
        if since:
            query += " AND transaction_date >= ?"
            params.append(since)
        query += " ORDER BY transaction_date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return _rows_to_dicts(
        rows,
        [
            "id",
            "owner_name",
            "owner_title",
            "security_title",
            "transaction_date",
            "transaction_code",
            "shares_transacted",
            "price_per_share",
            "shares_held_after",
            "direct_or_indirect",
        ],
    )


@mcp.tool()
def get_insider_holdings(
    ticker: str, as_of: str | None = None, limit: int = 100
) -> list[dict[str, object]]:
    """Return insider holdings snapshots (forms 3/4/5) for a ticker.

    Args:
        ticker: Ticker symbol.
        as_of: Optional date filter; returns holdings with ref_date <= as_of.
        limit: Maximum rows.

    Returns:
        List of {id, owner_name, owner_title, security_title, ref_date,
        shares_held_after, direct_or_indirect, form_type} ordered by
        ref_date desc.
    """
    with _connect() as conn:
        query = """
            SELECT id, owner_name, owner_title, security_title, ref_date,
                   shares_held_after, direct_or_indirect, form_type
            FROM insider_holdings
            WHERE ticker = ?
        """
        params: list[object] = [ticker]
        if as_of:
            query += " AND ref_date <= ?"
            params.append(as_of)
        query += " ORDER BY ref_date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return _rows_to_dicts(
        rows,
        [
            "id",
            "owner_name",
            "owner_title",
            "security_title",
            "ref_date",
            "shares_held_after",
            "direct_or_indirect",
            "form_type",
        ],
    )


@mcp.tool()
def get_13f_holdings(
    cusip: str,
    as_of: str | None = None,
    min_value: float | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return 13F holdings rows for a CUSIP (point-in-time).

    Args:
        cusip: 9-digit issuer CUSIP.
        as_of: Optional quarter-end filter; returns report_period <= as_of.
        min_value: Optional minimum position value in USD.
        limit: Maximum rows.

    Returns:
        List of {filer_name, filer_cik, report_period, issuer_name, class_title,
        value_usd, shares, put_call} ordered by report_period desc then
        value_usd desc.
    """
    if not _13f_partitions_exist():
        return []
    with _connect() as conn:
        query = """
            SELECT filer_name, filer_cik, report_period, issuer_name, class_title,
                   value_usd, shares, put_call
            FROM read_parquet(?, union_by_name = true)
            WHERE cusip = ?
        """
        params: list[object] = [FILINGS_13F_GLOB, cusip]
        if as_of:
            query += " AND report_period <= ?"
            params.append(as_of)
        if min_value is not None:
            query += " AND value_usd >= ?"
            params.append(min_value)
        query += " ORDER BY report_period DESC, value_usd DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return _rows_to_dicts(
        rows,
        [
            "filer_name",
            "filer_cik",
            "report_period",
            "issuer_name",
            "class_title",
            "value_usd",
            "shares",
            "put_call",
        ],
    )


@mcp.tool()
def get_macro(
    series: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, object]]:
    """Return macro series observations (point-in-time rows).

    Args:
        series: Macro series id from the registry (e.g. 'DGS10', 'VIXCLS',
            'EURUSD', 'ESTR'). See data/macro/series.py for the full list.
        start: Optional earliest observation date (YYYY-MM-DD).
        end: Optional latest observation date (YYYY-MM-DD). Pass end=as_of
            for point-in-time reads.

    Returns:
        List of {series, name, unit, frequency, date, value} ordered by date.
        Empty if the series is unknown or its parquet file is missing.
    """
    from data.macro.series import get_series
    from data.macro.store import series_path

    meta = get_series(series)
    if meta is None:
        return []
    path = series_path(series, MACRO_DIR)
    if not path.exists():
        return []

    conn = duckdb.connect()
    try:
        query = "SELECT date, value FROM read_parquet(?)"
        params: list[object] = [str(path)]
        if start or end:
            query += " WHERE date BETWEEN ? AND ?"
            params += [start or "1900-01-01", end or "2999-12-31"]
        query += " ORDER BY date"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    records = _rows_to_dicts(rows, ["date", "value"])
    for record in records:
        record.update(
            {
                "series": meta.id,
                "name": meta.name,
                "unit": meta.unit,
                "frequency": meta.frequency,
            }
        )
    return records


@mcp.tool()
def get_dividends(
    ticker: str,
    since: str | None = None,
    as_of: str | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return dividend history for a ticker (point-in-time by ex-date).

    Args:
        ticker: Yahoo symbol (e.g. 'AAPL', 'ASML.AS').
        since: Optional earliest ex-date (YYYY-MM-DD).
        as_of: Optional latest ex-date (YYYY-MM-DD); pass as_of for
            point-in-time reads.
        limit: Maximum rows.

    Returns:
        List of {symbol, ex_date, amount, currency} ordered by ex_date desc.
    """
    from data.dividends.store import symbol_path

    path = symbol_path(ticker, DIVIDENDS_DIR)
    if not path.exists():
        return []
    conn = duckdb.connect()
    try:
        query = "SELECT ex_date, amount, currency FROM read_parquet(?)"
        params: list[object] = [str(path)]
        if since or as_of:
            query += " WHERE ex_date BETWEEN ? AND ?"
            params += [since or "1900-01-01", as_of or "2999-12-31"]
        query += " ORDER BY ex_date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    records = _rows_to_dicts(rows, ["ex_date", "amount", "currency"])
    for record in records:
        record["symbol"] = ticker
    return records


@mcp.tool()
def get_short_interest(
    ticker: str,
    as_of: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return FINRA short interest observations for a ticker.

    Args:
        ticker: Normalized Yahoo symbol (e.g. 'AAPL', 'BRK-B').
        as_of: Optional settlement-date filter (YYYY-MM-DD); returns rows with
            settlement_date <= as_of.
        limit: Maximum rows.

    Returns:
        List of {symbol, issue_name, market_class, current_short,
        previous_short, avg_daily_volume, days_to_cover, change_pct,
        settlement_date} ordered by settlement_date desc.
    """
    import glob

    files = glob.glob(SHORT_INTEREST_GLOB)
    if not files:
        return []
    conn = duckdb.connect()
    try:
        query = """
            SELECT symbol, issue_name, market_class, current_short,
                   previous_short, avg_daily_volume, days_to_cover, change_pct,
                   settlement_date
            FROM read_parquet(?, union_by_name = true)
            WHERE symbol = ?
        """
        params: list[object] = [SHORT_INTEREST_GLOB, ticker.upper().replace(".", "-")]
        if as_of:
            query += " AND settlement_date <= ?"
            params.append(as_of)
        query += " ORDER BY settlement_date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(
        rows,
        [
            "symbol", "issue_name", "market_class", "current_short",
            "previous_short", "avg_daily_volume", "days_to_cover", "change_pct",
            "settlement_date",
        ],
    )


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
