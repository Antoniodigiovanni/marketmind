"""Tests for exchange-calendar helpers."""

from __future__ import annotations

import datetime as dt

from data.sources.calendars import exchange_for_suffix, is_trading_day


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
