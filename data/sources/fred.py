"""FRED (Federal Reserve Economic Data) source — macro indicators."""

from __future__ import annotations

import os

import polars as pl
import requests

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


def fetch_series(series_id: str, start: str, end: str) -> pl.DataFrame:
    """Fetch a macro series from FRED.

    Args:
        series_id: FRED series identifier (e.g. 'DGS10', 'CPIAUCSL').
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).

    Returns:
        DataFrame with columns [date, value] sorted by date. Missing
        observations ('.') are NULL.

    Raises:
        ValueError: If FRED_API_KEY env var is not set.
        requests.HTTPError: On non-2xx response from FRED.
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        raise ValueError("FRED_API_KEY env var is not set")

    response = requests.get(
        FRED_OBSERVATIONS_URL,
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start,
            "observation_end": end,
        },
        timeout=30,
    )
    response.raise_for_status()

    observations = response.json().get("observations", [])
    rows = [
        (obs["date"], None if obs.get("value") in (None, ".") else float(obs["value"]))
        for obs in observations
    ]
    if not rows:
        return pl.DataFrame(schema={"date": pl.Date, "value": pl.Float64})
    return (
        pl.DataFrame(rows, schema=["date", "value"], orient="row")
        .with_columns(pl.col("date").str.to_date())
        .sort("date")
    )
