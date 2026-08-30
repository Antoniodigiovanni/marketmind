"""Daily macro sync — incremental FRED + ECB fetch into data/raw/macro/."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl
from loguru import logger

from data.macro.series import MACRO_SERIES, MacroSeries
from data.macro.store import (
    MACRO_DIR,
    append_series,
    last_observation_date,
    read_series,
)
from data.processed.spreads import compute_hy_minus_bbb

FULL_HISTORY_START = "1900-01-01"
OVERLAP_DAYS = 30  # refetch buffer so monthly series revisions are caught


def _default_processed_dir() -> Path:
    """Return the configured directory for derived macro artifacts."""
    default_root = Path(__file__).resolve().parents[2] / "data" / "processed"
    return Path(os.getenv("MARKETMIND_PROCESSED_DIR", default_root)) / "macro"


PROCESSED_DIR = _default_processed_dir()
CREDIT_SPREADS_FILENAME = "credit_spreads.parquet"
HY_OAS_ID = "BAMLH0A0HYM2"
BBB_OAS_ID = "BAMLC0A4CBBB"


def fetch_series_observations(series: MacroSeries, start: str, end: str) -> pl.DataFrame:
    """Fetch observations for a series from its provider.

    Args:
        series: Series metadata from the registry.
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).

    Returns:
        DataFrame with columns [date, value].

    Raises:
        ValueError: If the series provider is unknown.
    """
    if series.provider == "fred":
        from data.sources import fred

        return fred.fetch_series(series.key, start, end)
    if series.provider == "ecb":
        from data.sources import ecb

        return ecb.fetch_series(series.key, start, end)
    raise ValueError(f"Unknown provider: {series.provider}")


def sync_series(series: MacroSeries, macro_dir: Path = MACRO_DIR, today: str | None = None) -> int:
    """Incrementally sync one series into its raw parquet file.

    Full backfill on first run; otherwise fetches from the last stored
    observation minus a 30-day overlap (dedupe keeps the latest value).

    Args:
        series: Series metadata from the registry.
        macro_dir: Root of the raw macro directory.
        today: Run date (YYYY-MM-DD); defaults to today.

    Returns:
        Number of rows appended.
    """
    end = today or dt.date.today().isoformat()
    last_date = last_observation_date(series.id, macro_dir)
    if last_date is None:
        start = FULL_HISTORY_START
    else:
        start = (last_date - dt.timedelta(days=OVERLAP_DAYS)).isoformat()

    frame = fetch_series_observations(series, start, end)
    if frame.is_empty():
        return 0
    return append_series(series.id, frame, macro_dir)


def refresh_credit_spreads(
    macro_dir: Path = MACRO_DIR, processed_dir: Path = PROCESSED_DIR
) -> pl.DataFrame:
    """Recompute and write the HY minus BBB OAS spread parquet.

    Args:
        macro_dir: Root of the raw macro directory.
        processed_dir: Directory for derived macro artifacts.

    Returns:
        The spread DataFrame (empty if either OAS series is missing).
    """
    empty = pl.DataFrame(
        schema={
            "date": pl.Date,
            "hy_oas": pl.Float64,
            "bbb_oas": pl.Float64,
            "hy_minus_bbb_oas": pl.Float64,
        }
    )
    hy = read_series(HY_OAS_ID, macro_dir)
    bbb = read_series(BBB_OAS_ID, macro_dir)
    if hy is None or bbb is None:
        logger.warning("Skipping credit spread refresh: HY or BBB OAS missing")
        return empty
    spread = compute_hy_minus_bbb(hy, bbb)
    processed_dir.mkdir(parents=True, exist_ok=True)
    spread.write_parquet(processed_dir / CREDIT_SPREADS_FILENAME)
    return spread


def run_daily_macro_sync(
    macro_dir: Path = MACRO_DIR,
    processed_dir: Path = PROCESSED_DIR,
    today: str | None = None,
) -> dict[str, int]:
    """Sync every registry series and refresh the derived credit spread.

    Per-series failures are logged and skipped so one bad source never aborts
    the run.

    Args:
        macro_dir: Root of the raw macro directory.
        processed_dir: Directory for derived macro artifacts.
        today: Run date (YYYY-MM-DD); defaults to today.

    Returns:
        Summary {series_id: rows appended} for successful series.
    """
    summary: dict[str, int] = {}
    for series in MACRO_SERIES:
        try:
            appended = sync_series(series, macro_dir, today)
        except Exception:
            logger.exception(f"{series.id}: sync failed, skipping")
            continue
        summary[series.id] = appended
        logger.info(f"{series.id}: {appended} new rows")

    spread = refresh_credit_spreads(macro_dir, processed_dir)
    logger.info(f"credit_spreads: {len(spread)} rows")
    return summary


def main() -> None:
    """Run the daily macro sync."""
    run_daily_macro_sync()


if __name__ == "__main__":
    main()
