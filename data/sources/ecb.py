"""ECB Statistical Data Warehouse source — EUR rates, FX, EA macro."""

from __future__ import annotations

import polars as pl


def fetch_series(series_key: str, start: str, end: str) -> pl.DataFrame:
    """Fetch a time series from the ECB SDW REST API.

    Args:
        series_key: ECB SDW series identifier (e.g. 'FM.B.U2.EUR.FR.BB.U2.EUR.MR.R').
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).

    Returns:
        DataFrame with columns [date, value]. DatetimeIndex is UTC.

    Raises:
        requests.HTTPError: On non-2xx response from ECB API.
    """
    raise NotImplementedError
