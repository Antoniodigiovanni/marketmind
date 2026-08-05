"""Tests for derived credit spread computation."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from data.processed.spreads import compute_hy_minus_bbb


def _frame(dates: list[dt.date], values: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {"date": dates, "value": values},
        schema={"date": pl.Date, "value": pl.Float64},
    )


def test_compute_hy_minus_bbb_known_output() -> None:
    dates = [dt.date(2026, 8, 3), dt.date(2026, 8, 4), dt.date(2026, 8, 5)]
    spread = compute_hy_minus_bbb(
        _frame(dates, [6.5, 7.0, 6.8]), _frame(dates, [1.5, 1.6, 1.7])
    )

    assert spread.columns == ["date", "hy_oas", "bbb_oas", "hy_minus_bbb_oas"]
    assert spread["hy_minus_bbb_oas"].to_list() == [5.0, 5.4, 5.1]
    assert spread["date"].to_list() == dates


def test_non_overlapping_dates_restrict_to_intersection() -> None:
    hy = _frame([dt.date(2026, 8, 3), dt.date(2026, 8, 4)], [6.5, 7.0])
    bbb = _frame([dt.date(2026, 8, 4), dt.date(2026, 8, 5)], [1.6, 1.7])

    spread = compute_hy_minus_bbb(hy, bbb)

    assert spread["date"].to_list() == [dt.date(2026, 8, 4)]
    assert spread["hy_minus_bbb_oas"].to_list() == [5.4]


def test_missing_columns_raise() -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        compute_hy_minus_bbb(
            pl.DataFrame({"date": [dt.date(2026, 8, 3)]}),
            _frame([dt.date(2026, 8, 3)], [1.5]),
        )
