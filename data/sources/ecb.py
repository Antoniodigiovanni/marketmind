"""ECB Statistical Data Warehouse source — EUR rates, FX, EA macro."""

from __future__ import annotations

import polars as pl
import requests

ECB_DATA_URL = "https://data-api.ecb.europa.eu/service/data"


def fetch_series(series_key: str, start: str, end: str) -> pl.DataFrame:
    """Fetch a time series from the ECB SDW REST API.

    Args:
        series_key: Full SDMX series path including dataflow, e.g.
            'EXR/D.USD.EUR.SP00.A'.
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).

    Returns:
        DataFrame with columns [date, value] sorted by date. Missing
        observations are NULL.

    Raises:
        requests.HTTPError: On non-2xx response from ECB API.
    """
    response = requests.get(
        f"{ECB_DATA_URL}/{series_key}",
        params={"startPeriod": start, "endPeriod": end, "format": "csvdata"},
        timeout=30,
    )
    response.raise_for_status()

    if not response.content.strip():
        return pl.DataFrame(schema={"date": pl.Date, "value": pl.Float64})
    frame = pl.read_csv(response.content)
    if frame.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, "value": pl.Float64})
    return frame.select(
        pl.col("TIME_PERIOD").str.to_date().alias("date"),
        pl.col("OBS_VALUE").cast(pl.Float64).alias("value"),
    ).sort("date")
