"""Short interest Parquet store — one file per settlement date."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl

REQUIRED_COLUMNS = [
    "symbol", "symbol_code", "issue_name", "market_class",
    "current_short", "previous_short", "avg_daily_volume",
    "days_to_cover", "change_pct", "settlement_date",
]
SHORT_INTEREST_DIR = (
    Path(__file__).resolve().parents[2] / "data" / "raw" / "short_interest"
)


def settlement_path(date: dt.date, si_dir: Path = SHORT_INTEREST_DIR) -> Path:
    """Return the parquet path for a settlement date."""
    return si_dir / f"short_interest_{date.isoformat()}.parquet"


def settlement_exists(date: dt.date, si_dir: Path = SHORT_INTEREST_DIR) -> bool:
    """Return True if the settlement date is already stored."""
    return settlement_path(date, si_dir).exists()


def write_settlement(
    date: dt.date, frame: pl.DataFrame, si_dir: Path = SHORT_INTEREST_DIR
) -> int:
    """Write one settlement date's short interest parquet (atomic).

    Returns:
        Number of rows written.

    Raises:
        ValueError: If frame has unexpected columns.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"short interest missing required columns: {missing}")
    path = settlement_path(date, si_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    frame.select(REQUIRED_COLUMNS).write_parquet(temp_path)
    os.replace(temp_path, path)
    return len(frame)
