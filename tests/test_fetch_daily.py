"""Tests for the daily price-fetch orchestrator (mocked, offline)."""

from __future__ import annotations

import datetime as dt

import polars as pl

import data.fetch as fetch_mod


def _fake_downloader(frame: pl.DataFrame) -> object:
    def downloader(*args: object, **kwargs: object) -> object:
        return frame

    return downloader


def test_fetch_daily_prices_writes_parquet(tmp_path, monkeypatch) -> None:
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    monkeypatch.setattr(
        fetch_mod,
        "get_universe",
        lambda as_of: pl.DataFrame(
            {
                "ticker": ["AAPL"],
                "name": ["Apple"],
                "country": ["US"],
                "sector": ["Technology"],
                "added_date": ["2026-01-01"],
                "removed_date": [None],
                "universe": ["sp500"],
            }
        ),
    )
    monkeypatch.setattr(
        fetch_mod,
        "symbols_for_universe",
        lambda conn, members: {"AAPL": "AAPL"},
    )
    frame = pl.DataFrame(
        {
            "date": [dt.datetime(2026, 8, 4, 20, 0, tzinfo=dt.UTC)],
            "ticker": ["AAPL"],
            "open": [100.0],
            "high": [102.0],
            "low": [99.0],
            "close": [101.0],
            "volume": [1000],
        }
    )
    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", lambda *a, **k: frame)

    summary = fetch_mod.fetch_daily_prices(
        as_of="2026-08-04",
        prices_dir=prices_dir,
        db_path=":memory:",
    )
    assert summary["fetched"] == ["AAPL"]
    written = pl.read_parquet(prices_dir / "AAPL.parquet")
    assert written.columns == ["date", "ticker", "open", "high", "low", "close", "volume"]
    assert written.height == 1


def test_fetch_daily_prices_skips_unresolved(tmp_path, monkeypatch) -> None:
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    monkeypatch.setattr(
        fetch_mod,
        "get_universe",
        lambda as_of: pl.DataFrame(
            {
                "ticker": ["NOPE"],
                "name": ["No Such"],
                "country": ["US"],
                "sector": ["X"],
                "added_date": ["2026-01-01"],
                "removed_date": [None],
                "universe": ["sp500"],
            }
        ),
    )
    monkeypatch.setattr(fetch_mod, "symbols_for_universe", lambda conn, members: {})

    summary = fetch_mod.fetch_daily_prices(
        as_of="2026-08-04", prices_dir=prices_dir, db_path=":memory:"
    )
    assert summary["fetched"] == []
    assert summary["skipped_unresolved"] == ["NOPE"]


def test_fetch_daily_prices_uses_last_trading_day(tmp_path, monkeypatch) -> None:
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    monkeypatch.setattr(
        fetch_mod,
        "get_universe",
        lambda as_of: pl.DataFrame(
            {
                "ticker": ["AAPL"],
                "name": ["Apple"],
                "country": ["US"],
                "sector": ["Technology"],
                "added_date": ["2026-01-01"],
                "removed_date": [None],
                "universe": ["sp500"],
            }
        ),
    )
    monkeypatch.setattr(
        fetch_mod, "symbols_for_universe", lambda conn, members: {"AAPL": "AAPL"}
    )
    captured: list[tuple[str, str]] = []

    def fake_ohlcv(tickers: object, start: str, end: str) -> pl.DataFrame:
        captured.append((start, end))
        return pl.DataFrame(
            {
                "date": [dt.datetime(2026, 8, 7, 20, 0, tzinfo=dt.UTC)],
                "ticker": ["AAPL"],
                "open": [100.0],
                "high": [102.0],
                "low": [99.0],
                "close": [101.0],
                "volume": [1000],
            }
        )

    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", fake_ohlcv)

    fetch_mod.fetch_daily_prices(
        as_of="2026-08-08",  # Saturday
        prices_dir=prices_dir,
        db_path=":memory:",
    )

    assert captured[0][1] == "2026-08-08"  # Friday close + 1 day
    assert not (prices_dir / "NOPE.parquet").exists()


def test_fetch_daily_prices_incremental_append(tmp_path, monkeypatch) -> None:
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    existing = pl.DataFrame(
        {
            "date": [dt.datetime(2026, 8, 3, 20, 0, tzinfo=dt.UTC)],
            "ticker": ["AAPL"],
            "open": [99.0],
            "high": [100.0],
            "low": [98.0],
            "close": [99.5],
            "volume": [900],
        }
    )
    existing.write_parquet(prices_dir / "AAPL.parquet")

    monkeypatch.setattr(
        fetch_mod,
        "get_universe",
        lambda as_of: pl.DataFrame(
            {
                "ticker": ["AAPL"],
                "name": ["Apple"],
                "country": ["US"],
                "sector": ["Technology"],
                "added_date": ["2026-01-01"],
                "removed_date": [None],
                "universe": ["sp500"],
            }
        ),
    )
    monkeypatch.setattr(fetch_mod, "symbols_for_universe", lambda conn, members: {"AAPL": "AAPL"})
    new_day = pl.DataFrame(
        {
            "date": [dt.datetime(2026, 8, 4, 20, 0, tzinfo=dt.UTC)],
            "ticker": ["AAPL"],
            "open": [100.0],
            "high": [102.0],
            "low": [99.0],
            "close": [101.0],
            "volume": [1000],
        }
    )
    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", lambda *a, **k: new_day)

    fetch_mod.fetch_daily_prices(as_of="2026-08-04", prices_dir=prices_dir, db_path=":memory:")
    written = pl.read_parquet(prices_dir / "AAPL.parquet")
    assert written.height == 2
    assert sorted(written["date"].dt.date().to_list()) == [dt.date(2026, 8, 3), dt.date(2026, 8, 4)]


def test_fetch_volatility_indices_writes_macro(tmp_path, monkeypatch) -> None:
    macro_dir = tmp_path / "macro"
    macro_dir.mkdir()
    frame = pl.DataFrame(
        {
            "date": [dt.datetime(2026, 8, 4, 20, 0, tzinfo=dt.UTC)] * 2,
            "ticker": ["^VIX", "^VSTOXX"],
            "open": [14.0, 16.0],
            "high": [14.5, 16.5],
            "low": [13.5, 15.5],
            "close": [14.2, 16.2],
            "volume": [0, 0],
        }
    )
    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", lambda *a, **k: frame)

    fetch_mod.fetch_volatility_indices(dt.date(2026, 8, 4), macro_dir=macro_dir)
    assert (macro_dir / "^VIX.parquet").exists()
    assert (macro_dir / "^VSTOXX.parquet").exists()
    vix = pl.read_parquet(macro_dir / "^VIX.parquet")
    assert vix["close"].to_list() == [14.2]
