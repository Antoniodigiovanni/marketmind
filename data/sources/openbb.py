"""OpenBB free-tier source — fundamentals, earnings, analyst estimates."""

from __future__ import annotations

import polars as pl


def fetch_fundamentals(ticker: str) -> pl.DataFrame:
    """Fetch fundamental data for a single ticker via OpenBB free tier.

    Args:
        ticker: Ticker symbol.

    Returns:
        DataFrame with fundamental columns. DatetimeIndex is UTC.

    Raises:
        NotImplementedError: Until OpenBB integration is wired.
    """
    raise NotImplementedError
