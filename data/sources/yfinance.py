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
            if sub.empty:
                continue
            sub.insert(0, "ticker", ticker)
            frames.append(sub)
    else:
        frame = data.reset_index().copy()
        if not frame.empty:
            frame.insert(0, "ticker", tickers[0])
            frames.append(frame)

    if not frames:
        return _empty_frame()

    long = pd.concat(frames, ignore_index=True).rename(columns=str.lower)
    if "date" not in long.columns and "index" in long.columns:
        long = long.rename(columns={"index": "date"})
    missing = [c for c in COLUMNS if c not in long.columns]
    if missing:
        raise ValueError(f"download missing required columns: {missing}")
    long["date"] = pd.to_datetime(long["date"], utc=True)
    long = long.dropna(subset=["close"])
    if long.empty:
        return _empty_frame()
    long["volume"] = long["volume"].astype("Int64")

    frame = pl.from_pandas(long[COLUMNS])
    return frame.with_columns(pl.col("date").dt.convert_time_zone("UTC"))


DIVIDEND_COLUMNS = ["ex_date", "amount"]


def fetch_dividends(
    symbol: str,
    start: str,
    end: str,
    *,
    history_fn: Callable[..., pd.DataFrame] | None = None,
) -> pl.DataFrame:
    """Fetch dividend history (ex-date, amount) for one symbol.

    Uses yfinance history with actions=True and keeps rows where the
    Dividends column is non-zero.

    Args:
        symbol: Yahoo symbol (e.g. 'AAPL', 'ASML.AS').
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).
        history_fn: Injectable history callable for tests.

    Returns:
        DataFrame with columns [ex_date DATE, amount DOUBLE] sorted by ex_date.

    Raises:
        ValueError: If the history frame lacks required columns.
    """
    if history_fn is None:

        def history_fn(**kwargs: object) -> pd.DataFrame:
            return yf.Ticker(symbol).history(**kwargs)

    history = history_fn(start=start, end=end, actions=True)

    empty = pl.DataFrame(schema={"ex_date": pl.Date, "amount": pl.Float64})
    if history is None or history.empty:
        return empty
    missing = [c for c in ("Dividends",) if c not in history.columns]
    if missing:
        raise ValueError(f"history missing required columns: {missing}")

    rows = history.loc[history["Dividends"].fillna(0.0) != 0.0]
    if rows.empty:
        return empty

    frame = pl.from_pandas(
        rows.reset_index()[["Date", "Dividends"]].rename(
            columns={"Date": "ex_date", "Dividends": "amount"}
        )
    )
    return frame.with_columns(pl.col("ex_date").dt.convert_time_zone("UTC").dt.date()).sort(
        "ex_date"
    )
