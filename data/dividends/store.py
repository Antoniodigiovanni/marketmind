"""Dividends Parquet store — per-symbol files under data/raw/dividends/."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl

REQUIRED_COLUMNS = ["ex_date", "amount", "currency"]
DIVIDENDS_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "dividends"


def symbol_path(symbol: str, dividends_dir: Path = DIVIDENDS_DIR) -> Path:
    """Return the parquet path for a Yahoo symbol."""
    return dividends_dir / f"{symbol}.parquet"


def read_symbol(symbol: str, dividends_dir: Path = DIVIDENDS_DIR) -> pl.DataFrame | None:
    """Read a symbol's dividend parquet, or None if missing."""
    path = symbol_path(symbol, dividends_dir)
    if not path.exists():
        return None
    frame = pl.read_parquet(path)
    _assert_schema(frame, symbol)
    return frame.sort("ex_date")


def append_symbol(symbol: str, frame: pl.DataFrame, dividends_dir: Path = DIVIDENDS_DIR) -> int:
    """Merge dividend observations into a symbol parquet (atomic).

    Deduplicates on ex_date (last value wins), sorts, and writes atomically.

    Returns:
        Number of rows appended.

    Raises:
        ValueError: If frame has unexpected columns.
    """
    _assert_schema(frame, symbol)
    existing = read_symbol(symbol, dividends_dir)
    before = 0 if existing is None else len(existing)
    merged = frame if existing is None else pl.concat([existing, frame])
    merged = merged.unique(subset=["ex_date"], keep="last").sort("ex_date").select(REQUIRED_COLUMNS)
    path = symbol_path(symbol, dividends_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    merged.write_parquet(temp_path)
    os.replace(temp_path, path)
    return len(merged) - before


def last_ex_date(symbol: str, dividends_dir: Path = DIVIDENDS_DIR) -> dt.date | None:
    """Return the latest ex-date for a symbol, or None."""
    existing = read_symbol(symbol, dividends_dir)
    if existing is None or existing.is_empty():
        return None
    return existing["ex_date"].max()


def _assert_schema(frame: pl.DataFrame, symbol: str) -> None:
    """Raise if frame lacks the required dividend columns."""
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{symbol} missing required columns: {missing}")
