"""Tests for the dividends Parquet store (tmp_path, no live calls)."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from data.dividends.store import append_symbol, last_ex_date, read_symbol, symbol_path


def _frame(dates: list[dt.date], amounts: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ex_date": dates,
            "amount": amounts,
            "currency": ["USD"] * len(dates),
        },
        schema={
            "ex_date": pl.Date,
            "amount": pl.Float64,
            "currency": pl.String,
        },
    )


def test_append_symbol_creates_new_file(tmp_path) -> None:
    appended = append_symbol(
        "AAPL", _frame([dt.date(2026, 2, 13)], [0.25]), tmp_path
    )

    assert appended == 1
    assert symbol_path("AAPL", tmp_path).exists()
    out = read_symbol("AAPL", tmp_path)
    assert out is not None
    assert out["ex_date"].to_list() == [dt.date(2026, 2, 13)]
    assert out["amount"].to_list() == [0.25]
    assert out["currency"].to_list() == ["USD"]


def test_append_symbol_dedupes_and_keeps_latest(tmp_path) -> None:
    append_symbol("AAPL", _frame([dt.date(2026, 2, 13)], [0.25]), tmp_path)
    appended = append_symbol(
        "AAPL",
        _frame([dt.date(2026, 2, 13), dt.date(2026, 5, 15)], [0.26, 0.25]),
        tmp_path,
    )

    assert appended == 1
    out = read_symbol("AAPL", tmp_path)
    assert out["ex_date"].to_list() == [dt.date(2026, 2, 13), dt.date(2026, 5, 15)]
    assert out["amount"].to_list() == [0.26, 0.25]


def test_read_symbol_missing_returns_none(tmp_path) -> None:
    assert read_symbol("NOPE", tmp_path) is None


def test_last_ex_date(tmp_path) -> None:
    assert last_ex_date("AAPL", tmp_path) is None
    append_symbol("AAPL", _frame([dt.date(2026, 5, 15)], [0.25]), tmp_path)
    assert last_ex_date("AAPL", tmp_path) == dt.date(2026, 5, 15)


def test_append_symbol_rejects_missing_columns(tmp_path) -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        append_symbol(
            "AAPL",
            pl.DataFrame({"ex_date": [dt.date(2026, 2, 13)]}),
            tmp_path,
        )
