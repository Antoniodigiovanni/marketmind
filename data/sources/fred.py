"""FRED (Federal Reserve Economic Data) source — macro indicators."""

from __future__ import annotations

import os

import polars as pl
from fredapi import Fred


def fetch_series(
    series_id: str,
    start: str,
    end: str,
    *,
    fred_client: Fred | None = None,
) -> pl.DataFrame:
    """Fetch a macro series from FRED.

    Args:
        series_id: FRED series identifier (e.g. 'DGS10', 'CPIAUCSL').
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).
        fred_client: Injectable Fred client for tests.

    Returns:
        DataFrame with columns [date, series_id, value]; dates are UTC.

    Raises:
        ValueError: If FRED_API_KEY env var is not set.
    """
    client = fred_client
    if client is None:
        api_key = os.environ.get("FRED_API_KEY")
        if not api_key:
            raise ValueError("FRED_API_KEY env var is not set")
        client = Fred(api_key=api_key)

    series = client.get_series(series_id, start=start, end=end)
    frame = pl.DataFrame(
        {
            "date": series.index,
            "series_id": [series_id] * len(series),
            "value": series.to_numpy(),
        }
    )
    return frame.with_columns(pl.col("date").dt.convert_time_zone("UTC"))
