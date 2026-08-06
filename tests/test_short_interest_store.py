"""Tests for the short interest Parquet store (tmp_path, no live calls)."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from data.short_interest.store import (
    settlement_exists,
    settlement_path,
    write_settlement,
)


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["A"],
            "symbol_code": ["A"],
            "issue_name": ["Agilent Technologies Inc."],
            "market_class": ["NYSE"],
            "current_short": [7_538_437.0],
            "previous_short": [5_331_733.0],
            "avg_daily_volume": [2_136_993.0],
            "days_to_cover": [3.53],
            "change_pct": [41.39],
            "settlement_date": [dt.date(2026, 7, 15)],
        }
    )


def test_write_settlement_and_exists(tmp_path) -> None:
    date = dt.date(2026, 7, 15)
    assert settlement_exists(date, tmp_path) is False

    rows = write_settlement(date, _frame(), tmp_path)

    assert rows == 1
    assert settlement_path(date, tmp_path).exists()
    assert settlement_exists(date, tmp_path) is True


def test_write_settlement_rejects_missing_columns(tmp_path) -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        write_settlement(
            dt.date(2026, 7, 15),
            pl.DataFrame({"symbol": ["A"]}),
            tmp_path,
        )
