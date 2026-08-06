"""Realized volatility computation from daily adjusted close prices."""

from __future__ import annotations

from pathlib import Path

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
        pl.col("close").log().diff().over("ticker").alias("log_return")
    )

    for window in windows:
        df = df.with_columns(
            pl.col("log_return")
            .rolling_std(window_size=window)
            .over("ticker")
            .mul(ANNUALIZATION**0.5)
            .mul(100)
            .alias(f"rv_{window}d")
        )

    return df.drop("log_return", "close").sort(["date", "ticker"])


def build_volatility_file(
    prices_dir: Path, output_path: Path, windows: tuple[int, ...] = (20, 60)
) -> pl.DataFrame:
    """Compute realized volatility from all price parquet files and write it.

    Args:
        prices_dir: Directory with one parquet per symbol (date/ticker/close).
        output_path: Destination parquet path.
        windows: Rolling windows in trading days.

    Returns:
        The volatility DataFrame (empty typed frame when no price files).

    Raises:
        ValueError: If a price file lacks required columns.
    """
    empty = pl.DataFrame(
        schema={
            "date": pl.Datetime("us", "UTC"),
            "ticker": pl.String,
            "rv_20d": pl.Float64,
            "rv_60d": pl.Float64,
        }
    )
    files = sorted(prices_dir.glob("*.parquet")) if prices_dir.exists() else []
    if not files:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        empty.write_parquet(output_path)
        return empty

    frames: list[pl.DataFrame] = []
    for path in files:
        frame = pl.read_parquet(path)
        missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise ValueError(f"{path.name} missing required columns: {missing}")
        frames.append(frame.select(REQUIRED_COLUMNS))

    vol = compute_realized_vol(pl.concat(frames), windows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vol.write_parquet(output_path)
    return vol
