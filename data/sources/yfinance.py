"""Yahoo Finance data source — OHLCV, adjusted prices, vol indices."""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd
import polars as pl
import yfinance as yf

COLUMNS = ["date", "ticker", "open", "high", "low", "close", "volume"]


def _empty_frame() -> pl.DataFrame:
    """Return an empty frame with the canonical OHLCV schema (UTC dates)."""
    return pl.DataFrame(
        schema={
            "date": pl.Datetime("us", "UTC"),
            "ticker": pl.String,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        }
    )


def fetch_ohlcv(
    tickers: list[str],
    start: str,
    end: str,
    *,
    downloader: Callable[..., pd.DataFrame] = yf.download,
) -> pl.DataFrame:
    """Fetch adjusted OHLCV + volume for a list of tickers.

    Args:
        tickers: Yahoo symbols (e.g. ['AAPL', 'ASML.AS']).
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).
        downloader: Injectable download callable for tests (default yf.download).

    Returns:
        DataFrame with columns [date, ticker, open, high, low, close, volume];
        dates are timezone-aware UTC.

    Raises:
        ValueError: If tickers is empty or required columns are missing.
    """
    if not tickers:
        raise ValueError("tickers must not be empty")

    data = downloader(
        tickers,
        start=start,
        end=end,
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )

    frames: list[pd.DataFrame] = []
    if isinstance(data.columns, pd.MultiIndex):
        for ticker in tickers:
            if ticker not in data.columns.get_level_values(0):
                continue
            sub = data[ticker].reset_index().copy()
            sub.insert(0, "ticker", ticker)
            frames.append(sub)
    else:
        frame = data.reset_index().copy()
        frame.insert(0, "ticker", tickers[0])
        frames.append(frame)

    if not frames:
        return _empty_frame()

    long = pd.concat(frames, ignore_index=True).rename(columns=str.lower)
    long["date"] = pd.to_datetime(long["date"], utc=True)
    missing = [c for c in COLUMNS if c not in long.columns]
    if missing:
        raise ValueError(f"download missing required columns: {missing}")

    frame = pl.from_pandas(long[COLUMNS]).drop_nulls(subset=["close"])
    return frame.with_columns(pl.col("date").dt.convert_time_zone("UTC"))
