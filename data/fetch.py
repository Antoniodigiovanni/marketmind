"""Orchestrates daily price/FX/volatility refresh against PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import math
import uuid

import polars as pl
from loguru import logger

from data.benchmarks import BENCHMARKS, Benchmark
from data.db.companies import create_company
from data.db.identifiers import resolve_identifier
from data.db.listings import get_current_listing, get_current_listing_for_company, upsert_listing
from data.db.pool import close_pool, dsn_from_env, open_pool
from data.db.prices import (
    get_last_fx_date,
    get_last_price_date,
    replace_all_volatility,
    upsert_fx_rates,
    upsert_prices,
)
from data.db.runs import finish_run, start_run
from data.dividends.fetch import sync_dividends
from data.macro.fetch import run_daily_macro_sync_postgres
from data.processed.volatility import compute_realized_vol
from data.short_interest.fetch import sync_short_interest_postgres
from data.sources.calendars import (
    CURRENCY_BY_SUFFIX,
    exchange_for_suffix,
    infer_currency,
    last_trading_day,
)
from data.sources.ecb import fetch_series as fetch_fx_series
from data.sources.yfinance import fetch_ohlcv
from data.universe.loader import get_universe

HISTORY_START = "2015-01-01"

# Non-equity volatility indices. These are not, and will never be, members
# of any universe -- nothing else in the identity-resolution pipeline will
# ever create listing rows for them, so this module bootstraps them itself.
# See docs/superpowers/specs/2026-08-11-postgres-app-port-phase5-market-data-design.md §2.2.
VOL_INDICES: dict[str, str] = {
    "^VIX": "CBOE Volatility Index",
    "^VSTOXX": "Euro Stoxx 50 Volatility Index",
}


async def resolve_symbol_listing(conn, symbol: str) -> uuid.UUID | None:
    """Resolve a Yahoo symbol to its live listing_id, or None if unresolved.

    Args:
        conn: An open connection.
        symbol: Yahoo symbol, e.g. 'AAPL' or 'ASML.AS'.

    Returns:
        The listing_id, or None if no live listing exists for this symbol
        yet. Unresolved is expected in normal operation until
        `data/universe/resolve.py` is cut over to write `listings`
        directly -- see the phase-5 design doc §2.3.
    """
    listing = await get_current_listing(conn, provider="yahoo", symbol=symbol)
    return listing["listing_id"] if listing else None


async def resolve_ticker_listing(
    conn, *, ticker: str, as_of: dt.date
) -> tuple[str, uuid.UUID] | None:
    """Resolve a point-in-time universe ticker to its current Yahoo listing."""
    company_id = await resolve_identifier(conn, id_type="ticker", id_value=ticker, as_of=as_of)
    if company_id is None:
        return None
    listing = await get_current_listing_for_company(conn, company_id=company_id, provider="yahoo")
    if listing is None or listing["status"] != "valid":
        return None
    return str(listing["symbol"]), listing["listing_id"]


async def ensure_index_listing(conn, *, symbol: str, name: str) -> uuid.UUID:
    """Get or create the synthetic company/listing for a non-equity index.

    Volatility, market, and sector indices are not equities and will never be
    members of a company universe, so nothing in the identity-resolution
    pipeline will create rows for them. This bootstraps exactly one synthetic
    company and one listing per symbol, idempotently -- see the phase-5 design
    doc §2.2.

    Args:
        conn: An open connection.
        symbol: Yahoo symbol, e.g. '^VIX' or '^GSPC'.
        name: Display name for the synthetic company row.

    Returns:
        The listing_id, existing or newly created.
    """
    existing = await get_current_listing(conn, provider="yahoo", symbol=symbol)
    if existing is not None:
        return existing["listing_id"]

    company_id = await create_company(
        conn,
        name=name,
        country=None,
        sector=None,
        first_seen=dt.date.fromisoformat(HISTORY_START),
    )
    return await upsert_listing(
        conn,
        company_id=company_id,
        provider="yahoo",
        symbol=symbol,
        status="valid",
        valid_from=dt.date.fromisoformat(HISTORY_START),
    )


async def fetch_benchmark_prices(conn, *, as_of: str) -> dict[str, list[str]]:
    """Incrementally fetch the configured market and sector benchmarks.

    Benchmark identities are bootstrapped through the same synthetic-company
    and Yahoo-listing convention as volatility indices. Fetches are grouped by
    incremental start date to keep the daily request count small.

    Args:
        conn: An open connection.
        as_of: Date string (YYYY-MM-DD) to fetch through, inclusive.

    Returns:
        Registry keys grouped as ``fetched``, ``current``, ``no_data``, or
        ``failed``. A failed group is logged and does not stop other refreshes.
    """
    as_of_date = dt.date.fromisoformat(as_of)
    end = (as_of_date + dt.timedelta(days=1)).isoformat()
    pending: dict[str, tuple[Benchmark, uuid.UUID, str]] = {}
    summary: dict[str, list[str]] = {
        "fetched": [],
        "current": [],
        "no_data": [],
        "failed": [],
    }

    for benchmark in BENCHMARKS:
        listing_id = await ensure_index_listing(conn, symbol=benchmark.symbol, name=benchmark.name)
        last_date = await get_last_price_date(conn, listing_id=listing_id)
        if last_date is not None and last_date >= as_of_date:
            summary["current"].append(benchmark.key)
            continue
        start = (last_date + dt.timedelta(days=1)).isoformat() if last_date else HISTORY_START
        pending[benchmark.key] = (benchmark, listing_id, start)

    groups: dict[str, list[str]] = {}
    for key, (_benchmark, _listing_id, start) in pending.items():
        groups.setdefault(start, []).append(key)

    for start, keys in groups.items():
        symbols = [pending[key][0].symbol for key in keys]
        try:
            frame = fetch_ohlcv(symbols, start, end)
        except Exception as exc:  # noqa: BLE001 - remaining refresh steps must still run
            logger.warning(f"benchmark fetch failed for {symbols}: {exc}")
            summary["failed"].extend(keys)
            continue
        for key in keys:
            benchmark, listing_id, _start = pending[key]
            rows = _rows_from_ohlcv(frame, benchmark.symbol, benchmark.currency)
            if not rows:
                summary["no_data"].append(key)
                continue
            await upsert_prices(conn, listing_id=listing_id, rows=rows)
            summary["fetched"].append(key)

    return summary


def _finite_number(value: object) -> object:
    """Return None for null/NaN floats, otherwise the original value."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _rows_from_ohlcv(frame: pl.DataFrame, symbol: str, currency: str) -> list[dict[str, object]]:
    """Convert one symbol's slice of a fetch_ohlcv frame into upsert_prices rows."""
    if frame.is_empty():
        return []
    subset = frame.filter(pl.col("ticker") == symbol)
    rows: list[dict[str, object]] = []
    for row in subset.to_dicts():
        close = _finite_number(row["close"])
        if close is None:
            continue
        value = row["date"]
        volume = _finite_number(row["volume"])
        rows.append(
            {
                "date": value.date() if hasattr(value, "date") else value,
                "open": _finite_number(row["open"]),
                "high": _finite_number(row["high"]),
                "low": _finite_number(row["low"]),
                "close": close,
                "volume": int(volume) if volume is not None else None,
                "currency": currency,
            }
        )
    return rows


async def fetch_daily_prices(
    conn,
    *,
    as_of: str,
    members: pl.DataFrame | None = None,
) -> dict[str, list[str]]:
    """Fetch daily OHLCV for every resolved universe member, incrementally.

    Listings that share an incremental start date (nearly all of them in
    steady state -- last run's as_of + 1) are fetched in one batched
    `fetch_ohlcv` call, so a full run issues roughly one request per
    distinct start date rather than one per ticker.

    Args:
        conn: An open connection.
        as_of: Date string (YYYY-MM-DD) to fetch through.
        members: Optional pre-loaded universe frame (tests); defaults to
            `get_universe(as_of)`.

    Returns:
        {"fetched": [ticker, ...], "skipped_unresolved": [ticker, ...]}.
    """
    as_of_date = dt.date.fromisoformat(as_of)
    members = members if members is not None else get_universe(as_of)

    # ticker -> (symbol, listing_id, incremental start date)
    resolved: dict[str, tuple[str, uuid.UUID, str]] = {}
    skipped: list[str] = []
    for row in members.to_dicts():
        ticker = row["ticker"]
        listing = await resolve_ticker_listing(conn, ticker=ticker, as_of=as_of_date)
        if listing is None:
            skipped.append(ticker)
            continue
        symbol, listing_id = listing
        last_date = await get_last_price_date(conn, listing_id=listing_id)
        start = (last_date + dt.timedelta(days=1)).isoformat() if last_date else HISTORY_START
        resolved[ticker] = (symbol, listing_id, start)

    groups: dict[str, list[str]] = {}
    for ticker, (_symbol, _listing_id, start) in resolved.items():
        groups.setdefault(start, []).append(ticker)

    fetched: list[str] = []
    for start, tickers in groups.items():
        symbols = [resolved[t][0] for t in tickers]
        suffix = symbols[0].rsplit(".", 1)[1] if "." in symbols[0] else ""
        trade_date = last_trading_day(exchange_for_suffix(suffix), as_of_date)
        end = (trade_date + dt.timedelta(days=1)).isoformat()
        try:
            frame = fetch_ohlcv(symbols, start, end)
        except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the run
            logger.warning(f"batched price fetch failed for {symbols}: {exc}")
            continue
        if frame.is_empty():
            continue
        for ticker, symbol in zip(tickers, symbols, strict=True):
            _symbol, listing_id, _start = resolved[ticker]
            currency = infer_currency(symbol) or "USD"
            rows = _rows_from_ohlcv(frame, symbol, currency)
            if not rows:
                continue
            await upsert_prices(conn, listing_id=listing_id, rows=rows)
            fetched.append(ticker)

    return {"fetched": fetched, "skipped_unresolved": skipped}


async def fetch_volatility_indices(conn, as_of: dt.date) -> None:
    """Fetch VIX/VSTOXX closing data into `prices`, bootstrapping listings.

    Args:
        conn: An open connection.
        as_of: Date to fetch through (inclusive).
    """
    end = (as_of + dt.timedelta(days=1)).isoformat()
    symbols = list(VOL_INDICES)
    frame = fetch_ohlcv(symbols, HISTORY_START, end)
    for symbol, name in VOL_INDICES.items():
        listing_id = await ensure_index_listing(conn, symbol=symbol, name=name)
        currency = "EUR" if symbol == "^VSTOXX" else "USD"
        rows = _rows_from_ohlcv(frame, symbol, currency)
        if rows:
            await upsert_prices(conn, listing_id=listing_id, rows=rows)


async def sync_fx_rates(conn, *, as_of: str) -> dict[str, int]:
    """Incrementally sync ECB EUR reference rates for every priced currency.

    Args:
        conn: An open connection.
        as_of: Date string (YYYY-MM-DD) to fetch through.

    Returns:
        Summary {quote_currency: rows written}.
    """
    as_of_date = dt.date.fromisoformat(as_of)
    end = (as_of_date + dt.timedelta(days=1)).isoformat()
    summary: dict[str, int] = {}
    quote_currencies = sorted({c for c in CURRENCY_BY_SUFFIX.values() if c != "EUR"})
    for quote in quote_currencies:
        last_date = await get_last_fx_date(conn, base_currency="EUR", quote_currency=quote)
        start = (last_date + dt.timedelta(days=1)).isoformat() if last_date else HISTORY_START
        try:
            frame = fetch_fx_series(f"EXR/D.{quote}.EUR.SP00.A", start, end)
        except Exception:
            logger.exception(f"fx sync failed for EUR/{quote}, skipping")
            continue
        frame = frame.drop_nulls(subset=["value"])
        if frame.is_empty():
            continue
        rows = [
            {"quote_currency": quote, "date": r["date"], "rate": r["value"], "source": "ecb"}
            for r in frame.to_dicts()
        ]
        written = await upsert_fx_rates(conn, base_currency="EUR", rows=rows)
        summary[quote] = written
        logger.info(f"fx EUR/{quote}: {written} new rows")
    return summary


async def refresh_volatility(conn) -> int:
    """Recompute realized volatility for every listing with priced bars.

    Args:
        conn: An open connection.

    Returns:
        Number of volatility rows written.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT listing_id FROM prices")
        listing_ids = [row[0] for row in await cur.fetchall()]

    if not listing_ids:
        await replace_all_volatility(conn, rows=[])
        return 0

    frames: list[pl.DataFrame] = []
    for listing_id in listing_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT date, close FROM prices WHERE listing_id = %s ORDER BY date", (listing_id,)
            )
            observations = await cur.fetchall()
        if not observations:
            continue
        frames.append(
            pl.DataFrame(
                {
                    "date": [o[0] for o in observations],
                    "ticker": [str(listing_id)] * len(observations),
                    "close": [o[1] for o in observations],
                }
            )
        )

    if not frames:
        await replace_all_volatility(conn, rows=[])
        return 0

    vol = compute_realized_vol(pl.concat(frames))
    # Every priced date gets a row, including the rolling windows' warm-up
    # period where rv_20d/rv_60d are still null -- matches the original
    # Parquet output (build_volatility_file), which never filtered rows out.
    rows = [
        {
            "listing_id": uuid.UUID(row["ticker"]),
            "date": row["date"],
            "rv_20d": row["rv_20d"],
            "rv_60d": row["rv_60d"],
        }
        for row in vol.to_dicts()
    ]
    return await replace_all_volatility(conn, rows=rows)


async def _run(as_of: str) -> None:
    """Run the daily price/FX/volatility refresh against Postgres.

    Raises:
        Exception: Whatever a refresh step raises, after the failure and the
            counters collected so far are recorded in agent_runs.
    """
    pool = await open_pool(dsn_from_env())
    try:
        async with pool.connection() as conn:
            run_id = await start_run(conn, run_date=dt.date.fromisoformat(as_of), job_type="fetch")
            # Filled in step by step rather than at the end, so a run that
            # dies halfway still records what it managed to complete.
            counters: dict[str, object] = {}
            try:
                summary = await fetch_daily_prices(conn, as_of=as_of)
                counters["prices"] = {
                    "fetched": len(summary["fetched"]),
                    "skipped_unresolved": len(summary["skipped_unresolved"]),
                }

                await fetch_volatility_indices(conn, dt.date.fromisoformat(as_of))

                benchmark_summary = await fetch_benchmark_prices(conn, as_of=as_of)
                counters["benchmarks"] = {
                    group: len(keys) for group, keys in benchmark_summary.items()
                }

                fx_summary = await sync_fx_rates(conn, as_of=as_of)
                counters["fx"] = dict(fx_summary)

                vol_written = await refresh_volatility(conn)
                counters["volatility_rows"] = vol_written

                macro_summary = await run_daily_macro_sync_postgres(conn, today=as_of)
                counters["macro"] = dict(macro_summary)

                dividend_summary = await sync_dividends(conn, as_of=as_of)
                counters["dividends"] = dict(dividend_summary)

                short_summary = await sync_short_interest_postgres(conn, as_of=as_of)
                counters["short_interest"] = dict(short_summary)
            except Exception as exc:
                await finish_run(
                    conn, run_id=run_id, status="failed", counters=counters, error=str(exc)
                )
                raise
            await finish_run(conn, run_id=run_id, status="succeeded", counters=counters)
            logger.info(
                f"prices fetched={len(summary['fetched'])} "
                f"skipped={len(summary['skipped_unresolved'])} "
                f"fx={fx_summary} volatility_rows={vol_written} "
                f"benchmarks={benchmark_summary} macro={macro_summary} "
                f"dividends={dividend_summary} short_interest={short_summary}"
            )
    finally:
        await close_pool(pool)


def main() -> None:
    """Run the daily price/FX/volatility/macro/dividends/short-interest refresh.

    Raw files remain as source archives; all queryable observations are
    upserted into PostgreSQL by the async `_run` bridge.
    """
    parser = argparse.ArgumentParser(description="Daily price + FX + volatility refresh")
    parser.add_argument("--as-of", default=dt.date.today().isoformat(), help="YYYY-MM-DD")
    args = parser.parse_args()

    asyncio.run(_run(args.as_of))


if __name__ == "__main__":
    main()
