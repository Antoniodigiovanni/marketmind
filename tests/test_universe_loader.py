"""Tests for point-in-time universe loading."""

from __future__ import annotations

import polars as pl
import pytest

from data.universe.loader import get_universe

SP500_ROWS = [
    "ticker,name,country,sector,added_date,removed_date",
    "AAPL,Apple Inc.,US,Technology,1990-01-01,",
    "INTC,Intel Corp.,US,Semiconductors,1990-01-01,2025-12-31",
]
EUROSTOXX_ROWS = [
    "ticker,name,country,sector,added_date,removed_date",
    "ASML,ASML Holding,NL,Semiconductors,2000-01-01,",
]


def _write_universe(tmp_path) -> None:
    (tmp_path / "sp500.csv").write_text("\n".join(SP500_ROWS) + "\n", encoding="utf-8")
    (tmp_path / "eurostoxx600.csv").write_text("\n".join(EUROSTOXX_ROWS) + "\n", encoding="utf-8")


def test_get_universe_filters_removed_tickers(tmp_path) -> None:
    _write_universe(tmp_path)
    universe = get_universe("2026-01-01", universe_dir=tmp_path)

    tickers = universe["ticker"].to_list()
    assert tickers == ["AAPL", "ASML"]
    assert "INTC" not in tickers


def test_get_universe_includes_future_removed_ticker(tmp_path) -> None:
    _write_universe(tmp_path)
    universe = get_universe("2024-01-01", universe_dir=tmp_path)
    assert set(universe["ticker"].to_list()) == {"AAPL", "ASML", "INTC"}


def test_get_universe_empty_before_add_dates(tmp_path) -> None:
    _write_universe(tmp_path)
    universe = get_universe("1980-01-01", universe_dir=tmp_path)
    assert universe.is_empty()


def test_get_universe_adds_universe_column(tmp_path) -> None:
    _write_universe(tmp_path)
    universe = get_universe("2026-01-01", universe_dir=tmp_path)
    assert set(universe["universe"].to_list()) == {"sp500", "eurostoxx600"}


def test_get_universe_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        get_universe("2026-01-01", universe_dir=tmp_path)


def test_get_universe_bad_columns_raise(tmp_path) -> None:
    (tmp_path / "sp500.csv").write_text("ticker,added_date\nAAPL,1990-01-01\n", encoding="utf-8")
    (tmp_path / "eurostoxx600.csv").write_text(
        "ticker,name,country,sector,added_date,removed_date\nASML,X,NL,S,2000-01-01,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required columns"):
        get_universe("2026-01-01", universe_dir=tmp_path)


def test_get_universe_invalid_date_raises(tmp_path) -> None:
    _write_universe(tmp_path)
    with pytest.raises(ValueError):
        get_universe("not-a-date", universe_dir=tmp_path)


def test_get_universe_returns_expected_schema(tmp_path) -> None:
    _write_universe(tmp_path)
    universe = get_universe("2026-01-01", universe_dir=tmp_path)
    assert universe.schema.names() == [
        "ticker",
        "name",
        "country",
        "sector",
        "added_date",
        "removed_date",
        "universe",
    ]
    assert isinstance(universe, pl.DataFrame)
