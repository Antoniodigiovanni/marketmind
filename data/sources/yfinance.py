"""Yahoo Finance data source — OHLCV, adjusted prices, basic fundamentals."""

from __future__ import annotations

import polars as pl


def fetch_ohlcv(tickers: list[str], start: str, end: str) -> pl.DataFrame:
    """Fetch adjusted OHLCV data from Yahoo Finance.

    Args:
        tickers: List of Yahoo Finance ticker symbols.
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).

    Returns:
        DataFrame with columns [date, ticker, open, high, low, close, volume].
        DatetimeIndex is timezone-aware UTC.

    Raises:
        ValueError: If tickers list is empty.
    """
    raise NotImplementedError
