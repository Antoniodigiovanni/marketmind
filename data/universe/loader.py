"""Point-in-time universe loading from CSV constituent lists."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

UNIVERSE_DIR = Path(__file__).parent
UNIVERSE_FILES = ("sp500.csv", "eurostoxx600.csv")
DELISTED_FILE = "delisted.csv"
REQUIRED_COLUMNS = ["ticker", "name", "country", "sector", "added_date", "removed_date"]
DELISTED_REQUIRED = REQUIRED_COLUMNS + ["universe"]


def get_universe(as_of: str, universe_dir: Path = UNIVERSE_DIR) -> pl.DataFrame:
    """Return point-in-time universe constituents valid on a date.

    Filters on added_date <= as_of AND
    (removed_date IS NULL OR removed_date > as_of).

    Args:
        as_of: Date string (YYYY-MM-DD).
        universe_dir: Directory containing the universe CSVs.

    Returns:
        Point-in-time constituents with an added `universe` column
        ('sp500' | 'eurostoxx600').

    Raises:
        ValueError: If as_of is not a valid date or a file has wrong columns.
        FileNotFoundError: If a universe file is missing.
    """
    as_of_date = dt.date.fromisoformat(as_of)

    frames: list[pl.DataFrame] = []
    for filename in UNIVERSE_FILES:
        path = universe_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Universe file not found: {path}")

        frame = pl.read_csv(path)
        missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise ValueError(f"{filename} missing required columns: {missing}")

        frame = frame.with_columns(
            pl.col("added_date").str.to_date(),
            pl.col("removed_date").str.to_date(),
            pl.lit(filename.removesuffix(".csv")).alias("universe"),
        )
        frames.append(frame)

    delisted_path = universe_dir / DELISTED_FILE
    if delisted_path.exists():
        delisted = pl.read_csv(delisted_path)
        missing = [c for c in DELISTED_REQUIRED if c not in delisted.columns]
        if missing:
            raise ValueError(f"{DELISTED_FILE} missing required columns: {missing}")
        delisted = delisted.with_columns(
            pl.col("added_date").str.to_date(),
            pl.col("removed_date").str.to_date(),
        )
        frames.append(delisted)

    universe = pl.concat(frames, how="diagonal_relaxed")
    return universe.filter(
        (pl.col("added_date") <= as_of_date)
        & (pl.col("removed_date").is_null() | (pl.col("removed_date") > as_of_date))
    )
