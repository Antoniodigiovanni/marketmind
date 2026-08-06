"""Orchestrates daily price/macro downloads and writes results to data/raw/."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import duckdb
import polars as pl
from loguru import logger

from data.db.init import DEFAULT_DB_PATH
from data.macro.fetch import run_daily_macro_sync
from data.processed.volatility import build_volatility_file
from data.sources.calendars import exchange_for_suffix, last_trading_day
from data.sources.yfinance import fetch_ohlcv
from data.universe.loader import UNIVERSE_DIR, get_universe

ROOT_DIR = Path(__file__).resolve().parent.parent
PRICES_DIR = ROOT_DIR / "data" / "raw" / "prices"
MACRO_DIR = ROOT_DIR / "data" / "raw" / "macro"
PROCESSED_VOL_PATH = ROOT_DIR / "data" / "processed" / "volatility.parquet"
HISTORY_START = "2015-01-01"

VOL_INDICES = ["^VIX", "^VSTOXX"]


def symbols_for_universe(
    conn: duckdb.DuckDBPyConnection,
    members: pl.DataFrame,
) -> dict[str, str]:
    """Map universe tickers to validated provider symbols via symbol_map.

    Args:
        conn: DuckDB connection with the symbol_map table.
        members: Universe members with a 'ticker' column.

    Returns:
        Mapping ticker -> Yahoo symbol for members with status 'valid'.
    """
    tickers = members["ticker"].to_list()
    rows = conn.execute(
        """
        SELECT id_value, symbol FROM symbol_map
        WHERE provider = 'yahoo' AND status = 'valid'
          AND id_value IN (SELECT unnest(?))
        """,
        [tickers],
    ).fetchall()
    return {id_value: symbol for id_value, symbol in rows}


def last_price_date(ticker: str, prices_dir: Path) -> dt.date | None:
    """Return the latest stored price date for a ticker, or None."""
    path = prices_dir / f"{ticker}.parquet"
    if not path.exists():
        return None
    frame = pl.read_parquet(path)
    return frame["date"].max().date()


def fetch_ticker_prices(
    ticker: str,
    symbol: str,
    as_of: dt.date,
    prices_dir: Path,
) -> bool:
    """Incrementally fetch and append prices for one ticker.

    Args:
        ticker: Universe ticker (parquet filename).
        symbol: Validated Yahoo symbol to download.
        as_of: Date to fetch through (inclusive).
        prices_dir: Directory holding per-ticker parquet files.

    Returns:
        True when new rows were written.
    """
    path = prices_dir / f"{ticker}.parquet"
    existing = pl.read_parquet(path) if path.exists() else None
    start = (
        (last_price_date(ticker, prices_dir) + dt.timedelta(days=1)).isoformat()
        if existing is not None
        else HISTORY_START
    )
    end = (as_of + dt.timedelta(days=1)).isoformat()

    new = fetch_ohlcv([symbol], start, end)
    if new.is_empty():
        return False

    merged = (
        pl.concat([existing, new]).unique(subset=["date", "ticker"], keep="last").sort("date")
        if existing is not None
        else new.sort("date")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.write_parquet(path)
    return True


def fetch_daily_prices(
    *,
    as_of: str,
    prices_dir: Path = PRICES_DIR,
    db_path: str | Path = DEFAULT_DB_PATH,
    universe_dir: Path = UNIVERSE_DIR,
    members: pl.DataFrame | None = None,
) -> dict[str, list[str]]:
    """Fetch daily OHLCV + volume for all universe members, incrementally.

    Args:
        as_of: Date string (YYYY-MM-DD) to fetch through.
        prices_dir: Parquet output directory.
        db_path: DuckDB path with the symbol_map table.
        universe_dir: Universe CSV directory.
        members: Optional pre-loaded universe frame (tests).

    Returns:
        Summary dict: {fetched, skipped_unresolved, errors}.
    """
    as_of_date = dt.date.fromisoformat(as_of)
    members = members if members is not None else get_universe(as_of)

    conn = duckdb.connect(str(db_path))
    try:
        symbols = symbols_for_universe(conn, members)
    finally:
        conn.close()

    fetched: list[str] = []
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []
    for row in members.to_dicts():
        ticker = row["ticker"]
        symbol = symbols.get(ticker) or row.get("yahoo_symbol")
        if not symbol:
            skipped.append(ticker)
            continue
        suffix = symbol.rsplit(".", 1)[1] if "." in symbol else ""
        trade_date = last_trading_day(exchange_for_suffix(suffix), as_of_date)
        try:
            if fetch_ticker_prices(ticker, symbol, trade_date, prices_dir):
                fetched.append(ticker)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the run
            errors.append((ticker, str(exc)))
            logger.warning(f"price fetch failed for {ticker}: {exc}")

    return {"fetched": fetched, "skipped_unresolved": skipped, "errors": errors}


def fetch_volatility_indices(as_of: dt.date, macro_dir: Path = MACRO_DIR) -> None:
    """Fetch VIX/VSTOXX closing data into data/raw/macro/."""
    end = (as_of + dt.timedelta(days=1)).isoformat()
    frame = fetch_ohlcv(VOL_INDICES, HISTORY_START, end)
    macro_dir.mkdir(parents=True, exist_ok=True)
    for symbol in VOL_INDICES:
        subset = frame.filter(pl.col("ticker") == symbol)
        if not subset.is_empty():
            subset.write_parquet(macro_dir / f"{symbol}.parquet")


def main() -> None:
    """Run the daily price/macro refresh."""
    parser = argparse.ArgumentParser(description="Daily price + macro refresh")
    parser.add_argument("--as-of", default=dt.date.today().isoformat(), help="YYYY-MM-DD")
    args = parser.parse_args()

    as_of_date = dt.date.fromisoformat(args.as_of)
    summary = fetch_daily_prices(as_of=args.as_of)
    build_volatility_file(PRICES_DIR, PROCESSED_VOL_PATH)
    fetch_volatility_indices(as_of_date)
    run_daily_macro_sync(today=args.as_of)
    logger.info(
        f"prices fetched={len(summary['fetched'])} "
        f"skipped={len(summary['skipped_unresolved'])} errors={len(summary['errors'])}"
    )


if __name__ == "__main__":
    main()
