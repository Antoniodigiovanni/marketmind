"""ECB Statistical Data Warehouse source — EUR rates, FX, EA macro."""

from __future__ import annotations

import polars as pl
import requests

ECB_SDW_URL = "https://sdw-wsrest.ecb.europa.eu/service/data"


def fetch_series(series_key: str, start: str, end: str) -> pl.DataFrame:
    """Fetch a time series from the ECB SDW REST API.

    Args:
        series_key: ECB SDW series key (e.g. 'FM.B.U2.EUR.FR.BB.U2.EUR.MR.R').
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).

    Returns:
        DataFrame with columns [date, series_id, value].

    Raises:
        requests.HTTPError: On non-2xx response from ECB API.
    """
    resp = requests.get(
        f"{ECB_SDW_URL}/{series_key}",
        params={"startPeriod": start, "endPeriod": end, "format": "jsondata"},
        timeout=30,
    )
    resp.raise_for_status()

    payload = resp.json()
    observations = next(iter(payload["dataSets"][0]["series"].values()))["observations"]
    dates = [
        value["id"][:10] for value in payload["structure"]["dimensions"]["observation"][0]["values"]
    ]
    values = [float(observations[str(i)][0]) for i in range(len(observations))]

    return pl.DataFrame(
        {
            "date": dates[: len(values)],
            "series_id": [series_key] * len(values),
            "value": values,
        }
    ).with_columns(pl.col("date").str.to_date())
