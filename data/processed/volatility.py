"""Realized volatility computation from daily adjusted close prices."""

from __future__ import annotations

import polars as pl

REQUIRED_COLUMNS = ["date", "ticker", "close"]
ANNUALIZATION = 252.0


def compute_realized_vol(
    prices: pl.DataFrame,
    windows: tuple[int, ...] = (20, 60),
) -> pl.DataFrame:
    """Compute rolling annualised realized volatility per ticker.

    Args:
        prices: DataFrame with columns [date, ticker, close] (adjusted close).
        windows: Rolling windows in trading days (default 20, 60).

    Returns:
        DataFrame with columns [date, ticker, rv_{window}d] in annualised %,
        sorted by date then ticker.

    Raises:
        ValueError: If required columns are missing.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in prices.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df = prices.sort(["ticker", "date"]).with_columns(
        (pl.col("close").log().diff()).alias("log_return")
    )

    for window in windows:
        df = df.with_columns(
            (
                pl.col("log_return").rolling_std(window_size=window) * (ANNUALIZATION**0.5) * 100
            ).alias(f"rv_{window}d")
        )

    return df.drop("log_return", "close").sort(["date", "ticker"])
