"""Tests for the yfinance dividends client (mocked history, no live calls)."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import polars as pl
import pytest

from data.sources.yfinance import fetch_dividends


def _history(dates: list[str], dividends: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"Dividends": dividends},
        index=pd.DatetimeIndex(pd.to_datetime(dates, utc=True), name="Date"),
    )


def test_fetch_dividends_filters_and_sorts() -> None:
    frame = fetch_dividends(
        "AAPL",
        "2026-01-01",
        "2026-12-31",
        history_fn=lambda **kwargs: _history(
            ["2026-02-13", "2026-05-15", "2026-08-07"],
            [0.25, 0.0, 0.25],
        ),
    )

    assert frame.columns == ["ex_date", "amount"]
    assert frame["ex_date"].to_list() == [
        dt.date(2026, 2, 13),
        dt.date(2026, 8, 7),
    ]
    assert frame["amount"].to_list() == [0.25, 0.25]


def test_fetch_dividends_empty_history() -> None:
    frame = fetch_dividends(
        "NOPE",
        "2026-01-01",
        "2026-12-31",
        history_fn=lambda **kwargs: pd.DataFrame(),
    )
    assert frame.is_empty()
    assert frame.schema == {"ex_date": pl.Date, "amount": pl.Float64}


def test_fetch_dividends_no_dividend_rows() -> None:
    frame = fetch_dividends(
        "NOPE",
        "2026-01-01",
        "2026-12-31",
        history_fn=lambda **kwargs: _history(["2026-01-05"], [0.0]),
    )
    assert frame.is_empty()


def test_fetch_dividends_missing_columns_raise() -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        fetch_dividends(
            "AAPL",
            "2026-01-01",
            "2026-12-31",
            history_fn=lambda **kwargs: pd.DataFrame({"Close": [1.0]}),
        )
