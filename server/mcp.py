"""MCP server exposing the marketmind database to its local client."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import secrets
import uuid
from collections.abc import Callable
from pathlib import Path

import duckdb
import polars as pl
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from psycopg_pool import AsyncConnectionPool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

import server.jobs as jobs
from agent.embedding import hash_embed
from agent.views import get_market_views as get_views
from agent.views import save_market_view as save_view
from data.db import prices as prices_db
from data.db.companies import get_company
from data.db.company_profiles import get_current_profile
from data.db.esef import get_european_filings as filings_get_european_filings
from data.db.facts import create_fact as facts_create_fact
from data.db.facts import get_current_facts as facts_get_current_facts
from data.db.filings import get_filings as filings_get_filings
from data.db.filings import search_filing_text as filings_search_filing_text
from data.db.identifiers import get_current_company_id, resolve_identifier
from data.db.init import DEFAULT_DB_PATH, init_db
from data.db.init_postgres import apply_schema
from data.db.listings import get_current_listing
from data.db.pool import dsn_from_env, open_pool
from data.db.runs import list_recent_runs
from data.sources.rss import NEWS_RECORD_FIELDS
from data.universe.loader import get_universe

MCP_PORT = int(os.getenv("MARKETMIND_MCP_PORT", "8000"))

# The process binds 0.0.0.0 -- Docker's port publishing forwards to a
# container's network interface, not its own internal 127.0.0.1, so a
# process bound only to loopback inside the container is unreachable even
# from the host despite `docker-compose.prod.yml` mapping the port to
# 127.0.0.1. That host-side mapping (not this bind address) is what keeps
# the service unreachable off-box; DNS-rebinding protection is configured
# explicitly here since FastMCP only auto-enables it when host is a
# loopback name, which "0.0.0.0" is not.
mcp = FastMCP(
    "marketmind",
    host="0.0.0.0",  # noqa: S104 -- see comment above; loopback-only enforced by the host port mapping
    port=MCP_PORT,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    ),
)

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DEFAULT_DB_PATH
# PRICES_DIR and PROCESSED_VOL_PATH (per-ticker OHLCV / realized-vol Parquet)
# are retired -- get_prices/get_volatility read PostgreSQL now (see
# data/db/prices.py). MACRO_DIR stays: get_macro is still Parquet-backed.
MACRO_DIR = ROOT_DIR / "data" / "raw" / "macro"
DIVIDENDS_DIR = ROOT_DIR / "data" / "raw" / "dividends"
SHORT_INTEREST_GLOB = str(ROOT_DIR / "data" / "raw" / "short_interest" / "*.parquet")
NEWS_GLOB = str(ROOT_DIR / "data" / "raw" / "news" / "*.ndjson")
FILINGS_13F_GLOB = str(ROOT_DIR / "data" / "raw" / "13f" / "**" / "*.parquet")

# Columns returned by the news tools. Every entry must exist in the on-disk
# record written by data.sources.rss.article_to_record — asserted in tests, so
# a producer/consumer drift fails the suite instead of the live tool.
NEWS_COLUMNS = ["url", "source", "ticker", "published_at", "title", "summary"]
assert set(NEWS_COLUMNS) <= set(NEWS_RECORD_FIELDS)


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


# Shared Postgres pool for every async MCP tool. Lazily opened on first use
# and never explicitly closed -- this server is a long-lived stdio process,
# so process exit reclaims it. Consolidated from three separate per-PR
# helpers (_get_pool, _pg_pool, _connect_pg) that each independently
# reinvented this after three parallel cutover PRs merged without
# reconciling them.
_PG_POOL: AsyncConnectionPool | None = None
_PG_POOL_LOCK = asyncio.Lock()


async def _pg_pool() -> AsyncConnectionPool:
    """Return the module-level Postgres pool, opening it on first use."""
    global _PG_POOL
    if _PG_POOL is None:
        async with _PG_POOL_LOCK:
            if _PG_POOL is None:
                _PG_POOL = await open_pool()
    return _PG_POOL


def _jsonify_pg_row(row: dict[str, object]) -> dict[str, object]:
    """Convert a psycopg dict_row's date/UUID values into JSON-friendly strings."""
    result: dict[str, object] = {}
    for key, value in row.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, dt.date):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


@mcp.tool()
async def get_prices(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    to_currency: str | None = None,
) -> list[dict[str, object]]:
    """Return daily OHLCV rows for a ticker.

    Args:
        ticker: Yahoo symbol, e.g. 'AAPL' or 'ASML.AS'.
        start: Optional start date (YYYY-MM-DD).
        end: Optional end date (YYYY-MM-DD).
        to_currency: Optional ISO 4217 code. When set, prices are converted
            using the most recent ECB reference rate on or before each
            bar's date. Omit for the bar's native currency (today's
            behavior, unchanged).

    Returns:
        List of {date, open, high, low, close, volume}.
    """
    pool = await _pg_pool()
    async with pool.connection() as conn:
        listing = await get_current_listing(conn, provider="yahoo", symbol=ticker)
        if listing is None:
            return []
        rows = await prices_db.get_prices(
            conn,
            listing_id=listing["listing_id"],
            start=dt.date.fromisoformat(start) if start else None,
            end=dt.date.fromisoformat(end) if end else None,
            to_currency=to_currency,
        )
    return _rows_to_dicts(
        [(r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows],
        ["date", "open", "high", "low", "close", "volume"],
    )


@mcp.tool()
async def get_volatility(
    ticker: str, start: str | None = None, end: str | None = None
) -> list[dict[str, object]]:
    """Return volatility data for a ticker or market vol index.

    Args:
        ticker: Yahoo symbol ('AAPL', 'ASML.AS') or a vol index ('^VIX', '^VSTOXX').
        start: Optional start date (YYYY-MM-DD).
        end: Optional end date (YYYY-MM-DD).

    Returns:
        For a stock: {date, ticker, rv_20d, rv_60d} (annualised %).
        For a vol index: {date, ticker, open, high, low, close, volume}
        (its raw daily bar -- there is no "volatility of a volatility
        index" concept).
    """
    pool = await _pg_pool()
    async with pool.connection() as conn:
        listing = await get_current_listing(conn, provider="yahoo", symbol=ticker)
        if listing is None:
            return []

        start_date = dt.date.fromisoformat(start) if start else None
        end_date = dt.date.fromisoformat(end) if end else None

        if ticker in ("^VIX", "^VSTOXX"):
            rows = await prices_db.get_prices(
                conn, listing_id=listing["listing_id"], start=start_date, end=end_date
            )
            records = _rows_to_dicts(
                [
                    (r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"])
                    for r in rows
                ],
                ["date", "open", "high", "low", "close", "volume"],
            )
            for record in records:
                record["ticker"] = ticker
            return records

        rows = await prices_db.get_volatility(
            conn, listing_id=listing["listing_id"], start=start_date, end=end_date
        )
    records = _rows_to_dicts(
        [(r["date"], r["rv_20d"], r["rv_60d"]) for r in rows], ["date", "rv_20d", "rv_60d"]
    )
    for record in records:
        record["ticker"] = ticker
    return records


@mcp.tool()
def get_news(ticker: str, since: str | None = None, limit: int = 50) -> list[dict[str, object]]:
    """Return news articles for a ticker from the raw ingest files.

    Args:
        ticker: Ticker symbol.
        since: Optional date filter (YYYY-MM-DD).
        limit: Maximum articles to return.

    Returns:
        List of {url, source, ticker, published_at, title, summary}.
    """
    import glob

    files = glob.glob(NEWS_GLOB)
    if not files:
        return []

    conn = duckdb.connect()
    try:
        query = f"""
            SELECT {", ".join(NEWS_COLUMNS)}
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
    return _rows_to_dicts(rows, NEWS_COLUMNS)


@mcp.tool()
def search_news(query: str, limit: int = 50) -> list[dict[str, object]]:
    """Full-text search across ingested news articles.

    Args:
        query: Free-text search terms.
        limit: Maximum articles to return.

    Returns:
        List of {url, source, ticker, published_at, title, summary} ranked
        by recency. Only the stored title and summary are searched — RSS
        never gives us the article body.
    """
    import glob

    files = glob.glob(NEWS_GLOB)
    if not files:
        return []

    conn = duckdb.connect()
    try:
        rows = conn.execute(
            f"""
            SELECT {", ".join(NEWS_COLUMNS)}
            FROM read_ndjson(?, union_by_name = true)
            WHERE title ILIKE ? OR summary ILIKE ?
            ORDER BY published_at DESC LIMIT ?
            """,
            [files, f"%{query}%", f"%{query}%", limit],
        ).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, NEWS_COLUMNS)


@mcp.tool()
async def get_filings(
    ticker: str,
    form_types: list[str] | None = None,
    filed_from: str | None = None,
    filed_to: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return SEC filing metadata for a ticker.

    Cut over to PostgreSQL: filings are now keyed by company_id rather
    than a raw ticker/CIK column, so ticker is resolved to whichever
    company currently holds it (data/db/identifiers.py::get_current_company_id).
    `cik` and `article_id` are no longer available on this row shape --
    the identity model replaces both (company_id) and fact-extraction
    queueing moved off this table entirely.

    Args:
        ticker: Ticker symbol, resolved to the company currently holding it.
        form_types: Optional form types to include (e.g. ['10-K', '8-K']).
        filed_from: Optional earliest filing date (YYYY-MM-DD).
        filed_to: Optional latest filing date (YYYY-MM-DD).
        limit: Maximum rows.

    Returns:
        List of {filing_id, company_id, accession, form_type, filed_at,
        period, url, status} ordered by filed_at desc. Empty list if the
        ticker doesn't resolve to a tracked company.
    """
    pool = await _pg_pool()
    async with pool.connection() as conn:
        company_id = await get_current_company_id(conn, id_type="ticker", id_value=ticker)
        if company_id is None:
            return []
        rows = await filings_get_filings(
            conn,
            company_id=company_id,
            form_types=form_types,
            filed_from=dt.date.fromisoformat(filed_from) if filed_from else None,
            filed_to=dt.date.fromisoformat(filed_to) if filed_to else None,
            limit=limit,
        )
    return [_jsonify_pg_row(row) for row in rows]


@mcp.tool()
async def get_european_filings(
    ticker: str | None = None,
    lei: str | None = None,
    country: str | None = None,
    filing_system: str | None = None,
    as_of: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return ESEF filing metadata for European universe members.

    Cut over to PostgreSQL: esef_filings is now keyed by company_id
    rather than a raw ticker column, so a ticker filter is resolved to
    whichever company currently holds it
    (data/db/identifiers.py::get_current_company_id).

    Args:
        ticker: Optional ticker symbol, resolved to the company currently
            holding it. Returns [] if it doesn't resolve to a tracked
            company -- a ticker filter that can't be resolved must not
            silently fall back to "no filter".
        lei: Optional issuer LEI.
        country: Optional ISO country code (e.g. 'IT', 'FR', 'ES').
        filing_system: Optional filing system ('ESEF' or 'UKSEF').
        as_of: Optional cutoff date (YYYY-MM-DD); returns filings with
            filed_at <= as_of.
        limit: Maximum rows.

    Returns:
        List of {esef_filing_id, fxo_id, lei, company_id, entity_name,
        country, filing_system, filed_at, period_end, package_url,
        xhtml_url, json_url, status} ordered by filed_at desc.
    """
    pool = await _pg_pool()
    async with pool.connection() as conn:
        company_id: uuid.UUID | None = None
        if ticker is not None:
            company_id = await get_current_company_id(conn, id_type="ticker", id_value=ticker)
            if company_id is None:
                return []
        rows = await filings_get_european_filings(
            conn,
            company_id=company_id,
            lei=lei,
            country=country,
            filing_system=filing_system,
            filed_to=dt.date.fromisoformat(as_of) if as_of else None,
            limit=limit,
        )
    return [_jsonify_pg_row(row) for row in rows]


@mcp.tool()
async def search_filings(
    query: str,
    ticker: str | None = None,
    form_types: list[str] | None = None,
    filed_from: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Full-text search across stored SEC filing text.

    Cut over to PostgreSQL: filings no longer carry a raw ticker column,
    so a ticker filter is resolved to whichever company currently holds
    it (data/db/identifiers.py::get_current_company_id).

    Args:
        query: Free-text search terms (all terms must match).
        ticker: Optional ticker filter, resolved to the company currently
            holding it. Returns [] if it doesn't resolve to a tracked
            company.
        form_types: Optional form types to include.
        filed_from: Optional earliest filing date (YYYY-MM-DD).
        limit: Maximum rows.

    Returns:
        List of {filing_id, company_id, form_type, filed_at, url, snippet}
        ordered by filed_at desc.
    """
    pool = await _pg_pool()
    async with pool.connection() as conn:
        company_id: uuid.UUID | None = None
        if ticker is not None:
            company_id = await get_current_company_id(conn, id_type="ticker", id_value=ticker)
            if company_id is None:
                return []
        rows = await filings_search_filing_text(
            conn,
            query=query,
            company_id=company_id,
            form_types=form_types,
            filed_from=dt.date.fromisoformat(filed_from) if filed_from else None,
            limit=limit,
        )
    return [_jsonify_pg_row(row) for row in rows]


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
            "symbol",
            "issue_name",
            "market_class",
            "current_short",
            "previous_short",
            "avg_daily_volume",
            "days_to_cover",
            "change_pct",
            "settlement_date",
        ],
    )


@mcp.tool()
async def get_facts(ticker: str, as_of: str) -> list[dict[str, object]]:
    """Return facts for a ticker as of a date (point-in-time safe).

    Args:
        ticker: Ticker symbol, resolved to whichever company held it as of
            `as_of` (not necessarily today's holder).
        as_of: Date string (YYYY-MM-DD) — required, never omitted.

    Returns:
        List of {fact_id, fact_type, statement, confidence, valid_from,
        valid_until, source_article}. Empty list if the ticker doesn't
        resolve to a tracked company as of `as_of`.

    Raises:
        ValueError: If as_of is missing or empty.
    """
    if not as_of:
        raise ValueError(
            "as_of is required — querying facts without a date is a look-ahead violation"
        )
    as_of_date = dt.date.fromisoformat(as_of)
    pool = await _pg_pool()
    async with pool.connection() as conn:
        company_id = await resolve_identifier(
            conn, id_type="ticker", id_value=ticker, as_of=as_of_date
        )
        if company_id is None:
            return []
        rows = await facts_get_current_facts(conn, company_id=company_id, as_of=as_of_date)
    return [_jsonify_pg_row(row) for row in rows]


@mcp.tool()
async def get_market_views(
    as_of: str,
    ticker: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return historical market views as of a date.

    Args:
        as_of: Date (YYYY-MM-DD); returns views for this date or earlier.
        ticker: Optional ticker filter; a ticker with no matching company
            as of `as_of` yields an empty result rather than an error.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        limit: Maximum views to return.

    Returns:
        List of view dicts (view_id, as_of, scope, company_id, theme,
        view_text, model, data_cutoff, used_fact_ids, created_at) ordered by
        as_of descending.
    """
    pool = await _pg_pool()
    async with pool.connection() as conn:
        return await get_views(conn, as_of=as_of, ticker=ticker, scope=scope, limit=limit)


@mcp.tool()
async def get_company_profile(
    ticker: str,
    as_of: str | None = None,
) -> dict[str, object] | None:
    """Return the generated company profile (description + categories) as of a date.

    Args:
        ticker: Company ticker, resolved to whichever company held it as of
            `as_of` (not necessarily today's holder).
        as_of: Date (YYYY-MM-DD); defaults to today. Profiles are versioned,
            so this returns the version valid on that date.

    Returns:
        Dict with ticker, name, sector, country, description,
        products_services, website_url, sources, valid_from, generated_by;
        None when the ticker doesn't resolve to a tracked company as of
        `as_of`, or no profile exists as of the date.
    """
    effective_as_of = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    pool = await _pg_pool()
    async with pool.connection() as conn:
        company_id = await resolve_identifier(
            conn, id_type="ticker", id_value=ticker, as_of=effective_as_of
        )
        if company_id is None:
            return None
        profile = await get_current_profile(conn, company_id=company_id, as_of=effective_as_of)
        if profile is None:
            return None
        company = await get_company(conn, company_id=company_id)
    return {
        "ticker": ticker,
        "name": company["name"] if company else None,
        "sector": company["sector"] if company else None,
        "country": company["country"] if company else None,
        "description": profile["description"],
        "products_services": profile["products_services"],
        "website_url": profile["website_url"],
        "sources": profile["sources"],
        "valid_from": profile["valid_from"].isoformat(),
        "generated_by": profile["generated_by"],
    }


@mcp.tool()
async def save_market_view_tool(
    as_of: str,
    view_text: str,
    model: str,
    data_cutoff: str,
    scope: str = "market",
    ticker: str | None = None,
    theme: str | None = None,
    used_fact_ids: list[str] | None = None,
) -> str:
    """Persist a free-text market view (immutable once written).

    Args:
        as_of: Market date (YYYY-MM-DD) the view refers to.
        view_text: Free-text view content.
        model: Model identifier that produced the view.
        data_cutoff: Date (YYYY-MM-DD) of the newest data used to write the
            view. Required.
        scope: 'market' | 'ticker' | 'theme'.
        ticker: Required when scope == 'ticker'; must resolve to a known
            company as of `as_of`.
        theme: Required when scope == 'theme'.
        used_fact_ids: company_facts ids that informed this view, optional.

    Returns:
        New view ID.
    """
    pool = await _pg_pool()
    async with pool.connection() as conn:
        return await save_view(
            conn,
            as_of=as_of,
            view_text=view_text,
            model=model,
            data_cutoff=data_cutoff,
            scope=scope,
            ticker=ticker,
            theme=theme,
            used_fact_ids=used_fact_ids,
        )


@mcp.tool()
async def save_fact(
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
        ticker: Ticker symbol, resolved to whichever company held it as of
            `valid_from` (not necessarily today's holder).
        statement: One-sentence fact statement.
        fact_type: Free-form category.
        confidence: 0.0-1.0.
        valid_from: Date the fact became valid (YYYY-MM-DD).
        source_article_id: FK to articles.article_id, as a string.
        supersedes_id: Optional ID of the fact this replaces, as a string.

    Returns:
        New fact_id, as a string.

    Raises:
        ValueError: If ticker does not resolve to a tracked company as of
            `valid_from`, confidence is outside [0, 1], or source_article_id
            / supersedes_id is not a well-formed UUID string.
    """
    valid_from_date = dt.date.fromisoformat(valid_from)
    source_article_uuid = uuid.UUID(source_article_id)
    supersedes_uuid = uuid.UUID(supersedes_id) if supersedes_id else None

    # Computed before checking out a pooled connection: hash_embed is
    # CPU-bound with no internal await, so running it while a connection is
    # held would block the event loop and tie up the pool for no DB work.
    vector = await hash_embed(statement)

    async def _precomputed_embed(_statement: str) -> list[float]:
        return vector

    pool = await _pg_pool()
    async with pool.connection() as conn:
        company_id = await resolve_identifier(
            conn, id_type="ticker", id_value=ticker, as_of=valid_from_date
        )
        if company_id is None:
            raise ValueError(f"ticker {ticker!r} does not resolve to a tracked company")
        fact_id = await facts_create_fact(
            conn,
            company_id=company_id,
            statement=statement,
            fact_type=fact_type,
            confidence=confidence,
            valid_from=valid_from_date,
            source_article_id=source_article_uuid,
            embed=_precomputed_embed,
            model="hash-ngram",
            supersedes_id=supersedes_uuid,
        )
    return str(fact_id)


def _build_llm() -> Callable[[list[dict[str, str]]], str]:
    """Return the chat callable used for extraction, from the environment.

    Raises:
        LLMError: When no API key is configured.
    """
    from agent.llm import ChatClient

    return ChatClient().complete


def _build_edgar_client() -> object:
    """Return the EDGAR client used by the filings refresh."""
    from data.sources.sec import EdgardClient

    return EdgardClient()


def _valid_date(value: str) -> bool:
    """Return True when `value` parses as an ISO date."""
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _universe_for(date: str, tickers: list[str] | None) -> tuple[pl.DataFrame, list[str]]:
    """Return the point-in-time universe, optionally narrowed to `tickers`.

    Args:
        date: Point-in-time date (YYYY-MM-DD).
        tickers: Optional ticker subset; matched case-insensitively.

    Returns:
        (filtered universe, requested tickers absent from the universe).
    """
    universe = get_universe(date)
    if not tickers:
        return universe, []
    wanted = [t.strip().upper() for t in tickers if t.strip()]
    filtered = universe.filter(pl.col("ticker").str.to_uppercase().is_in(wanted))
    known = {str(t).upper() for t in filtered["ticker"].to_list()}
    return filtered, [t for t in wanted if t not in known]


@mcp.tool()
def run_ingest(
    date: str,
    profiles_only: bool = False,
    backfill: bool = False,
    tickers: list[str] | None = None,
) -> str:
    """Start the daily news → facts ingest for a date, in the background.

    The pipeline takes hours over the full universe, so this returns as soon
    as the run is accepted. Poll get_ingest_status() for progress. Only one
    run may be in flight at a time.

    Args:
        date: Date string (YYYY-MM-DD) to ingest.
        profiles_only: Only ensure/refresh company profiles; skip news fetch,
            fact extraction, and aggregates.
        backfill: Build a new profile version for every company (annual
            refresh) instead of only filling in missing ones.
        tickers: Optional subset of the universe to ingest; the whole
            universe for that date when omitted.

    Returns:
        A message with the new run id, or a diagnostic explaining why no run
        was started (bad date, unknown tickers, missing API key, or another
        run already in flight).
    """
    from agent.ingestion import _default_profile_builder, run_daily_ingest_with_pool

    if not _valid_date(date):
        return f"Invalid date '{date}': expected YYYY-MM-DD."

    universe, unknown = _universe_for(date, tickers)
    if universe.height == 0:
        return f"No universe entries on {date} for: {', '.join(unknown or tickers or [])}."

    try:
        llm = _build_llm()
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        return f"Cannot start ingest: {exc}"

    def work(progress: jobs.ProgressCallback) -> dict[str, int]:
        return asyncio.run(
            run_daily_ingest_with_pool(
                date,
                universe=universe,
                llm=llm,
                profile_builder=_default_profile_builder(llm),
                backfill=backfill,
                profiles_only=profiles_only,
                progress=progress,
            )
        )

    try:
        run_id = jobs.start_run("news", work)
    except jobs.RunInProgress as exc:
        return f"{exc}; refusing to start a second."

    message = (
        f"Ingest started for {date} "
        f"(run_id={run_id}, {universe.height} tickers). Poll get_ingest_status()."
    )
    if unknown:
        message += f" Unknown tickers ignored: {', '.join(unknown)}."
    return message


@mcp.tool()
def run_filings_refresh(run_date: str = "") -> str:
    """Start the daily SEC filings fetch for the universe, in the background.

    Returns as soon as the run is accepted; poll get_ingest_status() for
    progress. Only one run may be in flight at a time.

    Args:
        run_date: Date string (YYYY-MM-DD); today when omitted.

    Returns:
        A message with the new run id, or a diagnostic explaining why no run
        was started.
    """
    from data.filings.fetch import run_daily_fetch_with_pool

    date = run_date or dt.date.today().isoformat()
    if not _valid_date(date):
        return f"Invalid date '{date}': expected YYYY-MM-DD."

    universe, _ = _universe_for(date, None)
    if universe.height == 0:
        return f"No universe entries on {date}."

    try:
        client = _build_edgar_client()
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        return f"Cannot start filings refresh: {exc}"

    def work(progress: jobs.ProgressCallback) -> dict[str, int]:
        # server/jobs.py runs `work` synchronously on a background thread;
        # run_daily_fetch_with_pool is async (it awaits the Postgres
        # document/metadata writes alongside the unchanged DuckDB ones),
        # so asyncio.run() bridges the two here -- an acceptable bridge for
        # now, per this cutover's scope (server/jobs.py itself is untouched).
        conn = init_db(DB_PATH)
        try:
            return asyncio.run(
                run_daily_fetch_with_pool(
                    conn,
                    client,
                    run_date=date,
                    root=ROOT_DIR,
                    universe=universe,
                    progress=progress,
                )
            )
        finally:
            conn.close()

    try:
        run_id = jobs.start_run("filings", work)
    except jobs.RunInProgress as exc:
        return f"{exc}; refusing to start a second."

    return (
        f"Filings refresh started for {date} "
        f"(run_id={run_id}, {universe.height} tickers). Poll get_ingest_status()."
    )


@mcp.tool()
async def get_ingest_status(limit: int = 5) -> dict[str, object]:
    """Report the in-flight job run, if any, and recent run history.

    Args:
        limit: Number of past runs to return from the audit log.

    Returns:
        A dict with two keys. 'run' describes the most recent run started
        through this server — run_id, job_type, state ('running',
        'succeeded' or 'failed'), started_at, finished_at, elapsed_seconds,
        tickers_done, tickers_total, counts, error — or None if this server
        has started none. 'history' holds the most recent agent_runs rows
        from PostgreSQL, newest first — run_id, run_date, job_type, status,
        started_at, finished_at, counters (a nested dict of per-job counts,
        not flattened into fixed keys), error. Only 'news'-job runs appear
        here today; 'filings' runs are still logged to a separate DuckDB
        table pending its own cutover, so they will not show up in this
        history until that lands.
    """
    record = jobs.current_run()
    pool = await _pg_pool()
    async with pool.connection() as conn:
        rows = await list_recent_runs(conn, limit=limit)
    return {
        "run": record.snapshot() if record else None,
        "history": [_jsonify_pg_row(row) for row in rows],
    }


class _BearerAuthMiddleware:
    """ASGI middleware requiring a static bearer token on every HTTP request.

    Plain ASGI, not Starlette's BaseHTTPMiddleware — that helper buffers
    the whole response before forwarding it, which breaks streaming/SSE
    bodies. streamable-http's transport depends on unbuffered streaming, so
    this passes scope/receive/send straight through unmodified once the
    token check passes.

    Deliberately not FastMCP's own `auth`/`token_verifier` constructor
    arguments: those model an OAuth 2.1 resource server (`AuthSettings`
    requires a real `issuer_url`) built for multi-tenant remote servers
    authorizing many distinct third-party clients. marketmind has exactly
    one caller (the local MCP client) — a shared-secret bearer check is
    the right amount of machinery for that, not a fake OAuth issuer.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        """Wrap `app`, rejecting any HTTP request without the matching token."""
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the Authorization header for http scopes; pass everything else through."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        presented = headers.get(b"authorization", b"").decode("latin-1")
        if not secrets.compare_digest(presented, self._expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def main() -> None:
    """Run the MCP server over streamable-http.

    On every start (including a restart: unless-stopped recovery): applies
    pending Postgres migrations and ensures research.duckdb exists. Both are
    idempotent, so running them unconditionally here is what replaces the
    old deploy tooling's one-time `data.db.init`/`data.db.init_postgres`
    steps — initialisation is the app's own responsibility now, not
    something deploy scripting does once ahead of time.
    """
    asyncio.run(apply_schema(dsn_from_env()))
    init_db()

    token = os.getenv("MARKETMIND_MCP_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MARKETMIND_MCP_TOKEN must be set to a nonblank value")
    app = mcp.streamable_http_app()
    app.add_middleware(_BearerAuthMiddleware, token=token)
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")  # noqa: S104 -- see mcp's construction comment above


if __name__ == "__main__":  # pragma: no cover - exercised by the container entry point
    main()
