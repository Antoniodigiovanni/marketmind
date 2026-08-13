"""Tests for exchange-calendar helpers."""

from __future__ import annotations

import datetime as dt

from data.sources.calendars import (
    SUFFIX_TO_EXCHANGE,
    exchange_for_suffix,
    infer_currency,
    is_trading_day,
    last_trading_day,
)


def test_exchange_for_suffix() -> None:
    assert exchange_for_suffix("") == "XNYS"
    assert exchange_for_suffix(".DE") == "XETR"
    assert exchange_for_suffix(".PA") == "XPAR"
    assert exchange_for_suffix(".L") == "XLON"
    assert exchange_for_suffix(".SW") == "XSWX"


def test_is_trading_day_weekday() -> None:
    assert is_trading_day("XNYS", dt.date(2026, 8, 4)) is True  # Tuesday


def test_is_trading_day_weekend() -> None:
    assert is_trading_day("XNYS", dt.date(2026, 8, 2)) is False  # Sunday
    assert is_trading_day("XETR", dt.date(2026, 8, 2)) is False


def test_last_trading_day_weekend() -> None:
    assert last_trading_day("XNYS", dt.date(2026, 8, 8)) == dt.date(2026, 8, 7)  # Sat -> Fri
    assert last_trading_day("XNYS", dt.date(2026, 8, 9)) == dt.date(2026, 8, 7)  # Sun -> Fri


def test_last_trading_day_nyse_holiday() -> None:
    # July 4th 2026 falls on Saturday; observed Friday 2026-07-03 is a NYSE holiday.
    assert last_trading_day("XNYS", dt.date(2026, 7, 3)) == dt.date(2026, 7, 2)


def test_last_trading_day_session_day() -> None:
    assert last_trading_day("XNYS", dt.date(2026, 8, 4)) == dt.date(2026, 8, 4)


def test_infer_currency_us_symbol_has_no_suffix() -> None:
    assert infer_currency("AAPL") == "USD"


def test_infer_currency_known_eu_suffixes() -> None:
    assert infer_currency("ASML.AS") == "EUR"
    assert infer_currency("HLMA.L") == "GBP"
    assert infer_currency("NOVN.SW") == "CHF"
    assert infer_currency("INVE-B.ST") == "SEK"
    assert infer_currency("NOVO-B.CO") == "DKK"
    assert infer_currency("EQNR.OL") == "NOK"


def test_infer_currency_covers_every_exchange_suffix() -> None:
    for suffix in SUFFIX_TO_EXCHANGE:
        if suffix == "":
            continue
        symbol = f"TEST{suffix}"
        assert infer_currency(symbol) is not None, f"no currency mapped for {suffix}"


def test_infer_currency_unknown_suffix_returns_none() -> None:
    assert infer_currency("TEST.XX") is None
