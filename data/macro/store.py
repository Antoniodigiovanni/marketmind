"""Macro Parquet store — incremental per-series files under data/raw/macro/."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl

REQUIRED_COLUMNS = ["date", "value"]
MACRO_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "macro"


def series_path(series_id: str, macro_dir: Path = MACRO_DIR) -> Path:
    """Return the parquet path for a series id.

    Args:
        series_id: Registry id (filename stem).
        macro_dir: Root of the raw macro directory.

    Returns:
        Path to the series parquet file.
    """
    return macro_dir / f"{series_id}.parquet"


def read_series(series_id: str, macro_dir: Path = MACRO_DIR) -> pl.DataFrame | None:
    """Read a series parquet file.

    Args:
        series_id: Registry id (filename stem).
        macro_dir: Root of the raw macro directory.

    Returns:
        DataFrame with columns [date, value] sorted by date, or None if the
        file does not exist.

    Raises:
        ValueError: If the file has unexpected columns.
    """
    path = series_path(series_id, macro_dir)
    if not path.exists():
        return None
    frame = pl.read_parquet(path)
    _assert_schema(frame, series_id)
    return frame.sort("date")


def append_series(series_id: str, frame: pl.DataFrame, macro_dir: Path = MACRO_DIR) -> int:
    """Merge new observations into a series parquet file (atomic).

    Deduplicates on date (last value wins), sorts, and writes atomically via
    a temp file so a crash never leaves a truncated parquet.

    Args:
        series_id: Registry id (filename stem).
        frame: New observations with columns [date, value].
        macro_dir: Root of the raw macro directory.

    Returns:
        Number of rows appended (merged length minus previous length).

    Raises:
        ValueError: If frame has unexpected columns.
    """
    _assert_schema(frame, series_id)
    existing = read_series(series_id, macro_dir)
    before = 0 if existing is None else len(existing)
    merged = frame if existing is None else pl.concat([existing, frame])
    merged = merged.unique(subset=["date"], keep="last").sort("date").select(REQUIRED_COLUMNS)

    path = series_path(series_id, macro_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    merged.write_parquet(temp_path)
    os.replace(temp_path, path)
    return len(merged) - before


def last_observation_date(series_id: str, macro_dir: Path = MACRO_DIR) -> dt.date | None:
    """Return the latest observation date for a series.

    Args:
        series_id: Registry id (filename stem).
        macro_dir: Root of the raw macro directory.

    Returns:
        Latest date in the file, or None if the file is missing or empty.
    """
    existing = read_series(series_id, macro_dir)
    if existing is None or existing.is_empty():
        return None
    return existing["date"].max()


def _assert_schema(frame: pl.DataFrame, series_id: str) -> None:
    """Raise if frame lacks the required macro columns."""
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{series_id} missing required columns: {missing}")
