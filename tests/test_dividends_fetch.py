"""Tests for the dividend sync job (mocked yfinance, tmp_path storage)."""

from __future__ import annotations

import datetime as dt

import polars as pl

import data.dividends.fetch as div_mod
from data.dividends.store import read_symbol


def _div_frame(dates: list[dt.date], amounts: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {"ex_date": dates, "amount": amounts},
        schema={"ex_date": pl.Date, "amount": pl.Float64},
    )


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={"ex_date": pl.Date, "amount": pl.Float64})


def _members() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ticker": ["AAPL"],
            "name": ["Apple"],
            "country": ["US"],
            "sector": ["Technology"],
            "added_date": ["2026-01-01"],
            "removed_date": [None],
            "universe": ["sp500"],
        }
    )


def test_infer_currency() -> None:
    assert div_mod.infer_currency("AAPL") == "USD"
    assert div_mod.infer_currency("ASML.AS") == "EUR"
    assert div_mod.infer_currency("SHEL.L") == "GBP"
    assert div_mod.infer_currency("UNKNOWN.XX") is None


def test_sync_dividends_backfills_and_appends(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_fetch(symbol: str, start: str, end: str) -> pl.DataFrame:
        calls.append((symbol, start, end))
        if symbol == "AAPL":
            return _div_frame([dt.date(2026, 2, 13), dt.date(2026, 5, 15)], [0.25, 0.25])
        return _empty()

    monkeypatch.setattr(div_mod, "fetch_dividends", fake_fetch)
    monkeypatch.setattr(div_mod, "_symbols_for_universe", lambda db, members: {"AAPL": "AAPL"})

    summary = div_mod.sync_dividends(
        as_of="2026-08-04",
        dividends_dir=tmp_path,
        db_path=":memory:",
        members=_members(),
    )

    assert summary == {"AAPL": 2}
    assert calls[0][1] == "1900-01-01"
    out = read_symbol("AAPL", tmp_path)
    assert out is not None
    assert out["currency"].to_list() == ["USD", "USD"]

    calls.clear()

    def record_fetch(symbol: str, start: str, end: str) -> pl.DataFrame:
        calls.append((symbol, start, end))
        if symbol == "AAPL":
            return _div_frame([dt.date(2026, 5, 15), dt.date(2026, 8, 7)], [0.25, 0.26])
        return _empty()

    monkeypatch.setattr(
        div_mod,
        "fetch_dividends",
        record_fetch,
    )
    summary = div_mod.sync_dividends(
        as_of="2026-08-04",
        dividends_dir=tmp_path,
        db_path=":memory:",
        members=_members(),
    )
    assert calls[0][1] == "2026-04-15"  # 2026-05-15 minus 30 days
    assert summary == {"AAPL": 1}
    out = read_symbol("AAPL", tmp_path)
    assert out["ex_date"].to_list() == [
        dt.date(2026, 2, 13),
        dt.date(2026, 5, 15),
        dt.date(2026, 8, 7),
    ]


def test_sync_dividends_skips_unresolved_and_isolates_failures(tmp_path, monkeypatch) -> None:
    def fake_fetch(symbol: str, start: str, end: str) -> pl.DataFrame:
        if symbol == "AAPL":
            raise RuntimeError("boom")
        return _empty()

    monkeypatch.setattr(div_mod, "fetch_dividends", fake_fetch)
    monkeypatch.setattr(div_mod, "_symbols_for_universe", lambda db, members: {"AAPL": "AAPL"})

    summary = div_mod.sync_dividends(
        as_of="2026-08-04",
        dividends_dir=tmp_path,
        db_path=":memory:",
        members=_members(),
    )

    assert summary == {}


def test_fetch_main_includes_dividends(monkeypatch) -> None:
    import data.fetch as fetch_entry

    called: list[str] = []

    def fake_prices(**kwargs: object) -> dict[str, list[str]]:
        called.append("prices")
        return {"fetched": [], "skipped_unresolved": [], "errors": []}

    monkeypatch.setattr(fetch_entry, "fetch_daily_prices", fake_prices)
    monkeypatch.setattr(fetch_entry, "fetch_volatility_indices", lambda as_of: called.append("vol"))
    monkeypatch.setattr(
        fetch_entry, "run_daily_macro_sync", lambda **kwargs: called.append("macro")
    )
    monkeypatch.setattr(fetch_entry, "sync_dividends", lambda **kwargs: called.append("dividends"))
    monkeypatch.setattr("sys.argv", ["data/fetch.py", "--as-of", "2026-08-04"])

    fetch_entry.main()

    assert called == ["prices", "vol", "macro", "dividends"]
