"""Daily short interest sync — FINRA biweekly files into data/raw/short_interest/."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from loguru import logger

from data.short_interest.store import (
    SHORT_INTEREST_DIR,
    settlement_exists,
    write_settlement,
)
from data.sources.finra import fetch_short_interest_file

PROBE_DAYS = 20


def sync_settlement(date: dt.date, si_dir: Path = SHORT_INTEREST_DIR) -> int:
    """Fetch and store one settlement date if new.

    Returns:
        Number of rows ingested (0 if absent or already stored).
    """
    if settlement_exists(date, si_dir):
        return 0
    frame = fetch_short_interest_file(date)
    if frame.is_empty():
        return 0
    return write_settlement(date, frame, si_dir)


def sync_short_interest(*, as_of: str, si_dir: Path = SHORT_INTEREST_DIR) -> dict[str, int]:
    """Probe recent settlement dates and ingest any new FINRA files.

    Args:
        as_of: Date string (YYYY-MM-DD) to probe through.
        si_dir: Short interest output directory.

    Returns:
        Summary {settlement_date: rows ingested}.
    """
    as_of_date = dt.date.fromisoformat(as_of)
    summary: dict[str, int] = {}
    for offset in range(PROBE_DAYS + 1):
        date = as_of_date - dt.timedelta(days=offset)
        try:
            rows = sync_settlement(date, si_dir)
        except Exception:
            logger.exception(f"short interest sync failed for {date}, skipping")
            continue
        if rows:
            summary[date.isoformat()] = rows
            logger.info(f"short_interest {date}: {rows} rows")
    return summary
