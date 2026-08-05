"""Tests for the Yahoo Finance OHLCV fetcher (mocked downloads)."""

from __future__ import annotations

import pandas as pd
import polars as pl
import pytest

from data.sources.yfinance import fetch_ohlcv


def _multi_ticker_frame() -> pd.DataFrame:
    index = pd.to_datetime(["2026-08-03 20:00:00+00:00", "2026-08-04 20:00:00+00:00"]).rename(
        "Date"
    )
    data = {
        ("AAPL", "Open"): [100.0, 101.0],
        ("AAPL", "High"): [102.0, 103.0],
        ("AAPL", "Low"): [99.0, 100.0],
        ("AAPL", "Close"): [101.0, 102.0],
        ("AAPL", "Volume"): [1000, 1200],
        ("MSFT", "Open"): [200.0, 201.0],
        ("MSFT", "High"): [202.0, 203.0],
        ("MSFT", "Low"): [199.0, 200.0],
        ("MSFT", "Close"): [201.0, 202.0],
        ("MSFT", "Volume"): [2000, 2200],
    }
    return pd.DataFrame(data, index=index)


def test_fetch_ohlcv_normalises_multi_ticker_frame() -> None:
    def fake_downloader(*args: object, **kwargs: object) -> pd.DataFrame:
        return _multi_ticker_frame()

    df = fetch_ohlcv(["AAPL", "MSFT"], "2026-08-01", "2026-08-05", downloader=fake_downloader)

    assert df.columns == ["date", "ticker", "open", "high", "low", "close", "volume"]
    assert df.height == 4
    assert df["ticker"].to_list() == ["AAPL", "AAPL", "MSFT", "MSFT"]
    assert df["date"].dtype.time_zone == "UTC"
    assert df.filter(pl.col("ticker") == "AAPL")["close"].to_list() == [101.0, 102.0]


def test_fetch_ohlcv_empty_tickers_raises() -> None:
    with pytest.raises(ValueError, match="tickers"):
        fetch_ohlcv([], "2026-08-01", "2026-08-05")


def test_fetch_ohlcv_drops_missing_ticker() -> None:
    def fake_downloader(*args: object, **kwargs: object) -> pd.DataFrame:
        return _multi_ticker_frame()[["AAPL"]]

    df = fetch_ohlcv(["AAPL", "MSFT"], "2026-08-01", "2026-08-05", downloader=fake_downloader)
    assert set(df["ticker"].unique().to_list()) == {"AAPL"}
