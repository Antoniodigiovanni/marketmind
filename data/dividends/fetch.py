"""Daily dividend sync into immutable Parquet history and PostgreSQL."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import psycopg
from loguru import logger

from data.db.identifiers import resolve_identifier
from data.db.listings import get_current_listing_for_company
from data.db.market_data import get_last_dividend_date, upsert_dividends
from data.dividends.store import DIVIDENDS_DIR, append_symbol
from data.sources.calendars import infer_currency
from data.sources.yfinance import fetch_dividends
from data.universe.loader import get_universe

FULL_HISTORY_START = "1900-01-01"
OVERLAP_DAYS = 30


async def sync_dividends(
    conn: psycopg.AsyncConnection,
    *,
    as_of: str,
    dividends_dir: Path = DIVIDENDS_DIR,
    members: pl.DataFrame | None = None,
) -> dict[str, int]:
    """Sync dividend history for every resolved universe member."""
    as_of_date = dt.date.fromisoformat(as_of)
    members = members if members is not None else get_universe(as_of)
    summary: dict[str, int] = {}

    for row in members.to_dicts():
        ticker = str(row["ticker"])
        company_id = await resolve_identifier(
            conn, id_type="ticker", id_value=ticker, as_of=as_of_date
        )
        if company_id is None:
            continue
        listing = await get_current_listing_for_company(
            conn, company_id=company_id, provider="yahoo"
        )
        if listing is None or listing["status"] != "valid":
            continue
        symbol = str(listing["symbol"])
        last = await get_last_dividend_date(conn, listing_id=listing["listing_id"])
        start = (
            FULL_HISTORY_START
            if last is None
            else (last - dt.timedelta(days=OVERLAP_DAYS)).isoformat()
        )
        try:
            frame = fetch_dividends(symbol, start, (as_of_date + dt.timedelta(days=1)).isoformat())
            if frame.is_empty():
                summary[ticker] = 0
                continue
            enriched = frame.with_columns(pl.lit(infer_currency(symbol)).alias("currency"))
            append_symbol(symbol, enriched, dividends_dir)
            written = await upsert_dividends(
                conn, listing_id=listing["listing_id"], rows=enriched.to_dicts()
            )
        except Exception:
            logger.exception(f"dividend sync failed for {ticker} ({symbol}), skipping")
            continue
        summary[ticker] = written
        logger.info(f"{ticker} ({symbol}): {written} dividend rows upserted")
    return summary
