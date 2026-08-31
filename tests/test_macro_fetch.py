"""Tests for the daily macro sync job (mocked sources, tmp_path storage)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

import data.macro.fetch as fetch_mod
from data.db.market_data import get_macro
from data.macro.series import MACRO_SERIES
from data.macro.store import append_series, read_series


def test_default_processed_dir_accepts_a_runtime_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MARKETMIND_PROCESSED_DIR", str(tmp_path))

    assert fetch_mod._default_processed_dir() == tmp_path / "macro"


def _frame(dates: list[dt.date], values: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {"date": dates, "value": values},
        schema={"date": pl.Date, "value": pl.Float64},
    )


def _empty_frame() -> pl.DataFrame:
    return pl.DataFrame(schema={"date": pl.Date, "value": pl.Float64})


def test_full_backfill_on_first_run(tmp_path, monkeypatch) -> None:
    captured: dict[str, tuple[str, str]] = {}
    dgs10 = _frame([dt.date(2026, 8, 3), dt.date(2026, 8, 4)], [4.1, 4.09])

    def fake_fetch(series: object, start: str, end: str) -> pl.DataFrame:
        assert isinstance(series, fetch_mod.MacroSeries)
        captured[series.id] = (start, end)
        return dgs10 if series.id == "DGS10" else _empty_frame()

    monkeypatch.setattr(fetch_mod, "fetch_series_observations", fake_fetch)

    summary = fetch_mod.run_daily_macro_sync(
        macro_dir=tmp_path, processed_dir=tmp_path / "proc", today="2026-08-04"
    )

    assert captured["DGS10"] == ("1900-01-01", "2026-08-04")
    assert set(summary) == {series.id for series in MACRO_SERIES}
    assert summary["DGS10"] == 2
    out = read_series("DGS10", tmp_path)
    assert out is not None
    assert out["value"].to_list() == [4.1, 4.09]


def test_incremental_sync_appends_only_new_dates(tmp_path, monkeypatch) -> None:
    append_series("DGS10", _frame([dt.date(2026, 8, 3)], [4.1]), tmp_path)
    captured: dict[str, tuple[str, str]] = {}

    def fake_fetch(series: object, start: str, end: str) -> pl.DataFrame:
        assert isinstance(series, fetch_mod.MacroSeries)
        captured[series.id] = (start, end)
        if series.id == "DGS10":
            return _frame([dt.date(2026, 8, 3), dt.date(2026, 8, 4)], [4.2, 4.09])
        return _empty_frame()

    monkeypatch.setattr(fetch_mod, "fetch_series_observations", fake_fetch)

    summary = fetch_mod.run_daily_macro_sync(
        macro_dir=tmp_path, processed_dir=tmp_path / "proc", today="2026-08-04"
    )

    assert captured["DGS10"][0] == "2026-07-04"  # last date minus 30-day overlap
    assert summary["DGS10"] == 1
    out = read_series("DGS10", tmp_path)
    assert out["date"].to_list() == [dt.date(2026, 8, 3), dt.date(2026, 8, 4)]
    assert out["value"].to_list() == [4.2, 4.09]  # revision wins


def test_monthly_series_overlap_catches_same_period_revision(tmp_path, monkeypatch) -> None:
    append_series("CPIAUCSL", _frame([dt.date(2026, 6, 1)], [320.0]), tmp_path)

    def fake_fetch(series: object, start: str, end: str) -> pl.DataFrame:
        assert isinstance(series, fetch_mod.MacroSeries)
        if series.id == "CPIAUCSL":
            return _frame([dt.date(2026, 6, 1), dt.date(2026, 7, 1)], [320.1, 322.0])
        return _empty_frame()

    monkeypatch.setattr(fetch_mod, "fetch_series_observations", fake_fetch)

    summary = fetch_mod.run_daily_macro_sync(
        macro_dir=tmp_path, processed_dir=tmp_path / "proc", today="2026-08-04"
    )

    assert summary["CPIAUCSL"] == 1
    out = read_series("CPIAUCSL", tmp_path)
    assert out["date"].to_list() == [dt.date(2026, 6, 1), dt.date(2026, 7, 1)]
    assert out["value"].to_list() == [320.1, 322.0]


def test_credit_spread_file_refreshed(tmp_path, monkeypatch) -> None:
    hy = _frame([dt.date(2026, 8, 4)], [6.5])
    bbb = _frame([dt.date(2026, 8, 4)], [1.5])

    def fake_fetch(series: object, start: str, end: str) -> pl.DataFrame:
        assert isinstance(series, fetch_mod.MacroSeries)
        if series.id == "BAMLH0A0HYM2":
            return hy
        if series.id == "BAMLC0A4CBBB":
            return bbb
        return _empty_frame()

    monkeypatch.setattr(fetch_mod, "fetch_series_observations", fake_fetch)

    fetch_mod.run_daily_macro_sync(
        macro_dir=tmp_path, processed_dir=tmp_path / "proc", today="2026-08-04"
    )

    spread = pl.read_parquet(tmp_path / "proc" / "credit_spreads.parquet")
    assert spread["hy_minus_bbb_oas"].to_list() == [5.0]


def test_per_series_failure_isolation(tmp_path, monkeypatch) -> None:
    def fake_fetch(series: object, start: str, end: str) -> pl.DataFrame:
        assert isinstance(series, fetch_mod.MacroSeries)
        if series.id == "DGS10":
            raise RuntimeError("boom")
        return _empty_frame()

    monkeypatch.setattr(fetch_mod, "fetch_series_observations", fake_fetch)

    summary = fetch_mod.run_daily_macro_sync(
        macro_dir=tmp_path, processed_dir=tmp_path / "proc", today="2026-08-04"
    )

    assert "DGS10" not in summary
    assert set(summary) == {series.id for series in MACRO_SERIES if series.id != "DGS10"}


def test_fetch_main_runs_all_daily_syncs(monkeypatch) -> None:
    import data.fetch as fetch_entry

    called: list[str] = []

    async def fake_run(as_of: str) -> None:
        called.append("market_data")

    monkeypatch.setattr(fetch_entry, "_run", fake_run)
    monkeypatch.setattr("sys.argv", ["data/fetch.py", "--as-of", "2026-08-04"])

    fetch_entry.main()

    assert called == ["market_data"]


async def test_postgres_sync_writes_archived_observations(pg_conn, monkeypatch) -> None:
    """The PostgreSQL wrapper persists every archived series observation."""
    frame = _frame([dt.date(2026, 8, 4)], [4.09])
    monkeypatch.setattr(fetch_mod, "run_daily_macro_sync", lambda **_kwargs: {"DGS10": 1})
    monkeypatch.setattr(
        fetch_mod,
        "read_series",
        lambda series_id, _directory: frame if series_id == "DGS10" else None,
    )

    summary = await fetch_mod.run_daily_macro_sync_postgres(pg_conn, today="2026-08-04")

    assert summary == {"DGS10": 1}
    rows = await get_macro(pg_conn, series_id="DGS10")
    assert rows[0]["value"] == 4.09
