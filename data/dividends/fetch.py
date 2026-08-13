"""Daily dividend sync — per-symbol yfinance history into data/raw/dividends/."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import polars as pl
from loguru import logger

from data.db.init import DEFAULT_DB_PATH
from data.dividends.store import DIVIDENDS_DIR, append_symbol, last_ex_date
from data.sources.calendars import infer_currency
from data.sources.yfinance import fetch_dividends
from data.universe.loader import UNIVERSE_DIR, get_universe

FULL_HISTORY_START = "1900-01-01"
OVERLAP_DAYS = 30


def _symbols_for_universe(db_path: str | Path, members: pl.DataFrame) -> dict[str, str]:
    """Map universe tickers to validated Yahoo symbols via symbol_map."""
    conn = duckdb.connect(str(db_path))
    try:
        tickers = members["ticker"].to_list()
        rows = conn.execute(
            """
            SELECT id_value, symbol FROM symbol_map
            WHERE provider = 'yahoo' AND status = 'valid'
              AND id_value IN (SELECT unnest(?))
            """,
            [tickers],
        ).fetchall()
    finally:
        conn.close()
    return {id_value: symbol for id_value, symbol in rows}


def sync_symbol(
    symbol: str,
    as_of: dt.date,
    dividends_dir: Path,
) -> int:
    """Incrementally sync dividends for one symbol.

    Returns:
        Number of rows appended.
    """
    end = (as_of + dt.timedelta(days=1)).isoformat()
    last = last_ex_date(symbol, dividends_dir)
    if last is None:
        start = FULL_HISTORY_START
    else:
        start = (last - dt.timedelta(days=OVERLAP_DAYS)).isoformat()

    frame = fetch_dividends(symbol, start, end)
    if frame.is_empty():
        return 0
    enriched = frame.with_columns(pl.lit(infer_currency(symbol)).alias("currency"))
    return append_symbol(symbol, enriched, dividends_dir)


def sync_dividends(
    *,
    as_of: str,
    dividends_dir: Path = DIVIDENDS_DIR,
    db_path: str | Path = DEFAULT_DB_PATH,
    universe_dir: Path = UNIVERSE_DIR,
    members: pl.DataFrame | None = None,
) -> dict[str, int]:
    """Sync dividend history for every universe member.

    Args:
        as_of: Date string (YYYY-MM-DD) to fetch through.
        dividends_dir: Parquet output directory.
        db_path: DuckDB path with the symbol_map table.
        universe_dir: Universe CSV directory.
        members: Optional pre-loaded universe frame (tests).

    Returns:
        Summary {ticker: rows appended} for successful symbols.
    """
    as_of_date = dt.date.fromisoformat(as_of)
    members = members if members is not None else get_universe(as_of)
    symbols = _symbols_for_universe(db_path, members)

    summary: dict[str, int] = {}
    for row in members.to_dicts():
        ticker = row["ticker"]
        symbol = symbols.get(ticker)
        if not symbol:
            continue
        try:
            appended = sync_symbol(symbol, as_of_date, dividends_dir)
        except Exception:
            logger.exception(f"dividend sync failed for {ticker} ({symbol}), skipping")
            continue
        summary[ticker] = appended
        logger.info(f"{ticker} ({symbol}): {appended} new dividend rows")
    return summary
