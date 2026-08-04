"""FRED (Federal Reserve Economic Data) source — macro indicators."""

from __future__ import annotations

import polars as pl


def fetch_series(series_id: str, start: str, end: str) -> pl.DataFrame:
    """Fetch a macro series from FRED.

    Args:
        series_id: FRED series identifier (e.g. 'DGS10', 'CPIAUCSL').
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).

    Returns:
        DataFrame with columns [date, value]. DatetimeIndex is UTC.

    Raises:
        ValueError: If FRED_API_KEY env var is not set.
    """
    raise NotImplementedError
