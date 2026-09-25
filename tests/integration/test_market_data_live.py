"""Live contract checks for prices, dividends, and short interest."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from data.sources.finra import COLUMNS as FINRA_COLUMNS
from data.sources.finra import fetch_short_interest_file
from data.sources.yfinance import fetch_dividends, fetch_ohlcv

pytestmark = pytest.mark.live


def test_live_yfinance_prices_contract() -> None:
    """Parse a stable historical OHLCV window through yfinance."""
    frame = fetch_ohlcv(["AAPL"], "2024-01-02", "2024-01-10")

    assert frame.columns == ["date", "ticker", "open", "high", "low", "close", "volume"]
    assert not frame.is_empty()
    assert frame["ticker"].unique().to_list() == ["AAPL"]
    assert isinstance(frame.schema["date"], pl.Datetime)
    assert frame.schema["date"].time_zone == "UTC"


def test_live_yfinance_dividends_contract() -> None:
    """Parse a historical dividend window through yfinance."""
    frame = fetch_dividends("AAPL", "2024-01-01", "2025-01-01")

    assert frame.columns == ["ex_date", "amount"]
    assert not frame.is_empty()
    assert frame["amount"].min() > 0


def test_live_finra_short_interest_contract() -> None:
    """Parse a published FINRA biweekly short-interest file."""
    frame = fetch_short_interest_file(dt.date(2024, 1, 12))

    assert frame.columns == list(FINRA_COLUMNS)
    assert not frame.is_empty()
    assert frame["settlement_date"].unique().to_list() == [dt.date(2024, 1, 12)]
