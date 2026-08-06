"""Tests for the macro Parquet store (tmp_path, no live calls)."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from data.macro.store import append_series, last_observation_date, read_series, series_path


def _frame(dates: list[dt.date], values: list[float | None]) -> pl.DataFrame:
    return pl.DataFrame(
        {"date": dates, "value": values},
        schema={"date": pl.Date, "value": pl.Float64},
    )


def test_append_series_creates_new_file(tmp_path) -> None:
    appended = append_series(
        "DGS10",
        _frame([dt.date(2026, 8, 3), dt.date(2026, 8, 4)], [1.0, 2.0]),
        tmp_path,
    )

    assert appended == 2
    assert series_path("DGS10", tmp_path).exists()
    out = read_series("DGS10", tmp_path)
    assert out is not None
    assert out["date"].to_list() == [dt.date(2026, 8, 3), dt.date(2026, 8, 4)]
    assert out["value"].to_list() == [1.0, 2.0]


def test_append_dedupes_and_keeps_latest_value(tmp_path) -> None:
    append_series(
        "DGS10",
        _frame([dt.date(2026, 8, 4), dt.date(2026, 8, 3)], [2.0, 1.0]),
        tmp_path,
    )
    appended = append_series(
        "DGS10",
        _frame([dt.date(2026, 8, 4), dt.date(2026, 8, 5)], [2.5, 3.0]),
        tmp_path,
    )

    assert appended == 1
    out = read_series("DGS10", tmp_path)
    assert out["date"].to_list() == [
        dt.date(2026, 8, 3),
        dt.date(2026, 8, 4),
        dt.date(2026, 8, 5),
    ]
    assert out["value"].to_list() == [1.0, 2.5, 3.0]


def test_read_series_missing_file_returns_none(tmp_path) -> None:
    assert read_series("NOPE", tmp_path) is None


def test_last_observation_date(tmp_path) -> None:
    assert last_observation_date("DGS10", tmp_path) is None
    append_series("DGS10", _frame([dt.date(2026, 8, 5)], [3.0]), tmp_path)
    assert last_observation_date("DGS10", tmp_path) == dt.date(2026, 8, 5)


def test_append_series_rejects_missing_columns(tmp_path) -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        append_series("DGS10", pl.DataFrame({"date": [dt.date(2026, 8, 3)]}), tmp_path)
