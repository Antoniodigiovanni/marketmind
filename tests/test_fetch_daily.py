"""Tests for the daily price/FX/volatility fetch orchestrator (Postgres)."""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager

import polars as pl
import pytest

import data.fetch as fetch_mod
from data.db.identifiers import create_identifier
from data.db.listings import get_current_listing, upsert_listing
from data.db.prices import get_last_fx_date, get_prices, get_volatility, upsert_prices
from data.db.runs import list_recent_runs

pytestmark = pytest.mark.asyncio


async def _listing(pg_conn, symbol: str = "AAPL") -> uuid.UUID:
    company_id = await create_identifier(
        pg_conn,
        id_type="ticker",
        id_value=symbol,
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name=symbol,
        country="US",
        sector="Technology",
    )
    return await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol=symbol,
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )


def _members(tickers: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ticker": tickers,
            "name": tickers,
            "country": ["US"] * len(tickers),
            "sector": ["Technology"] * len(tickers),
            "added_date": ["2026-01-01"] * len(tickers),
            "removed_date": [None] * len(tickers),
            "universe": ["sp500"] * len(tickers),
        }
    )


async def test_resolve_symbol_listing_returns_none_when_unresolved(pg_conn) -> None:
    assert await fetch_mod.resolve_symbol_listing(pg_conn, "NOPE") is None


async def test_resolve_symbol_listing_returns_listing_id(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    assert await fetch_mod.resolve_symbol_listing(pg_conn, "AAPL") == listing_id


async def test_fetch_daily_prices_writes_rows_for_resolved_listing(pg_conn, monkeypatch) -> None:
    await _listing(pg_conn)
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

    summary = await fetch_mod.fetch_daily_prices(
        pg_conn, as_of="2026-08-04", members=_members(["AAPL"])
    )
    assert summary["fetched"] == ["AAPL"]
    assert summary["skipped_unresolved"] == []

    listing = await get_current_listing(pg_conn, provider="yahoo", symbol="AAPL")
    rows = await get_prices(pg_conn, listing_id=listing["listing_id"])
    assert [r["close"] for r in rows] == [101.0]


async def test_fetch_daily_prices_skips_unresolved_symbols(pg_conn, monkeypatch) -> None:
    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", lambda *a, **k: pl.DataFrame())
    summary = await fetch_mod.fetch_daily_prices(
        pg_conn, as_of="2026-08-04", members=_members(["NOPE"])
    )
    assert summary["fetched"] == []
    assert summary["skipped_unresolved"] == ["NOPE"]


async def test_fetch_daily_prices_is_incremental(pg_conn, monkeypatch) -> None:
    listing_id = await _listing(pg_conn)
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 3),
                "open": 99.0,
                "high": 100.0,
                "low": 98.0,
                "close": 99.5,
                "volume": 900,
                "currency": "USD",
            }
        ],
    )
    captured: list[tuple[str, str]] = []

    def fake_ohlcv(tickers: object, start: str, end: str) -> pl.DataFrame:
        captured.append((start, end))
        return pl.DataFrame(
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

    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", fake_ohlcv)
    await fetch_mod.fetch_daily_prices(pg_conn, as_of="2026-08-04", members=_members(["AAPL"]))
    assert captured[0][0] == "2026-08-04"  # last stored date + 1 day

    rows = await get_prices(pg_conn, listing_id=listing_id)
    assert len(rows) == 2


async def test_fetch_daily_prices_batches_shared_start_dates_into_one_call(
    pg_conn, monkeypatch
) -> None:
    await _listing(pg_conn, symbol="AAPL")
    await _listing(pg_conn, symbol="MSFT")
    calls: list[list[str]] = []

    def fake_ohlcv(tickers: object, start: str, end: str) -> pl.DataFrame:
        calls.append(list(tickers))
        return pl.DataFrame(
            {
                "date": [dt.datetime(2026, 8, 4, 20, 0, tzinfo=dt.UTC)] * len(tickers),
                "ticker": list(tickers),
                "open": [100.0] * len(tickers),
                "high": [102.0] * len(tickers),
                "low": [99.0] * len(tickers),
                "close": [101.0] * len(tickers),
                "volume": [1000] * len(tickers),
            }
        )

    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", fake_ohlcv)
    await fetch_mod.fetch_daily_prices(
        pg_conn, as_of="2026-08-04", members=_members(["AAPL", "MSFT"])
    )
    # Both listings have no prior bars, so both share HISTORY_START --
    # one batched call, not two.
    assert len(calls) == 1
    assert sorted(calls[0]) == ["AAPL", "MSFT"]


async def test_ensure_index_listing_is_idempotent(pg_conn) -> None:
    first = await fetch_mod.ensure_index_listing(
        pg_conn, symbol="^VIX", name="CBOE Volatility Index"
    )
    second = await fetch_mod.ensure_index_listing(
        pg_conn, symbol="^VIX", name="CBOE Volatility Index"
    )
    assert first == second


async def test_fetch_volatility_indices_writes_prices(pg_conn, monkeypatch) -> None:
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

    await fetch_mod.fetch_volatility_indices(pg_conn, dt.date(2026, 8, 4))

    vix_listing = await get_current_listing(pg_conn, provider="yahoo", symbol="^VIX")
    rows = await get_prices(pg_conn, listing_id=vix_listing["listing_id"])
    assert rows[0]["close"] == 14.2


async def test_fetch_benchmark_prices_bootstraps_and_writes_all_series(
    pg_conn, monkeypatch
) -> None:
    calls: list[tuple[list[str], str, str]] = []

    def fake_ohlcv(symbols: list[str], start: str, end: str) -> pl.DataFrame:
        calls.append((symbols, start, end))
        return pl.DataFrame(
            {
                "date": [dt.datetime(2026, 8, 4, 20, 0, tzinfo=dt.UTC)] * len(symbols),
                "ticker": symbols,
                "open": [100.0] * len(symbols),
                "high": [102.0] * len(symbols),
                "low": [99.0] * len(symbols),
                "close": [101.0] * len(symbols),
                "volume": [0] * len(symbols),
            }
        )

    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", fake_ohlcv)
    summary = await fetch_mod.fetch_benchmark_prices(pg_conn, as_of="2026-08-04")

    assert len(calls) == 1
    assert calls[0][1:] == (fetch_mod.HISTORY_START, "2026-08-05")
    assert set(summary["fetched"]) == {benchmark.key for benchmark in fetch_mod.BENCHMARKS}
    stoxx = await get_current_listing(pg_conn, provider="yahoo", symbol="^STOXX")
    rows = await get_prices(pg_conn, listing_id=stoxx["listing_id"])
    assert rows[0]["close"] == 101.0
    assert rows[0]["date"] == dt.date(2026, 8, 4)


async def test_fetch_benchmark_prices_is_incremental_and_reports_current(
    pg_conn, monkeypatch
) -> None:
    benchmark = fetch_mod.BENCHMARKS[0]
    listing_id = await fetch_mod.ensure_index_listing(
        pg_conn, symbol=benchmark.symbol, name=benchmark.name
    )
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 4),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 0,
                "currency": benchmark.currency,
            }
        ],
    )

    def fake_ohlcv(symbols: list[str], start: str, end: str) -> pl.DataFrame:
        assert benchmark.symbol not in symbols
        return pl.DataFrame()

    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", fake_ohlcv)
    summary = await fetch_mod.fetch_benchmark_prices(pg_conn, as_of="2026-08-04")

    assert summary["current"] == [benchmark.key]
    assert len(summary["no_data"]) == len(fetch_mod.BENCHMARKS) - 1


async def test_fetch_benchmark_prices_isolates_source_failure(pg_conn, monkeypatch) -> None:
    def fail(*args: object, **kwargs: object) -> pl.DataFrame:
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(fetch_mod, "fetch_ohlcv", fail)
    summary = await fetch_mod.fetch_benchmark_prices(pg_conn, as_of="2026-08-04")

    assert set(summary["failed"]) == {benchmark.key for benchmark in fetch_mod.BENCHMARKS}


async def test_sync_fx_rates_writes_and_is_incremental(pg_conn, monkeypatch) -> None:
    def fake_fetch_series(series_key: str, start: str, end: str) -> pl.DataFrame:
        return pl.DataFrame({"date": [dt.date(2026, 8, 4)], "value": [1.1]})

    monkeypatch.setattr(fetch_mod, "fetch_fx_series", fake_fetch_series)
    summary = await fetch_mod.sync_fx_rates(pg_conn, as_of="2026-08-04")
    assert summary["USD"] == 1
    assert await get_last_fx_date(pg_conn, base_currency="EUR", quote_currency="USD") == dt.date(
        2026, 8, 4
    )


async def test_refresh_volatility_computes_from_prices(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, day),
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0 + day,
                "volume": 1,
                "currency": "USD",
            }
            for day in range(1, 5)
        ],
    )
    written = await fetch_mod.refresh_volatility(pg_conn)
    assert written == 4
    rows = await get_volatility(pg_conn, listing_id=listing_id)
    assert len(rows) == 4


class _FakePool:
    """Hands the daily refresh the test's own transaction-scoped connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def connection(self):
        conn = self._conn

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm()


def _stub_refresh_steps(monkeypatch, pg_conn, *, failing: str | None = None) -> None:
    """Replace every refresh step with a canned summary, optionally failing one."""

    async def fake_open_pool(*args, **kwargs):
        return _FakePool(pg_conn)

    async def fake_close_pool(pool) -> None:
        return None

    monkeypatch.setattr(fetch_mod, "open_pool", fake_open_pool)
    monkeypatch.setattr(fetch_mod, "close_pool", fake_close_pool)

    steps = {
        "fetch_daily_prices": {"fetched": ["AAPL", "MSFT"], "skipped_unresolved": ["NOPE"]},
        "fetch_volatility_indices": None,
        "fetch_benchmark_prices": {
            "fetched": ["sp500"],
            "current": [],
            "no_data": [],
            "failed": ["stoxx600"],
        },
        "sync_fx_rates": {"USD": 3},
        "refresh_volatility": 7,
        "run_daily_macro_sync_postgres": {"DGS10": 2},
        "sync_dividends": {"AAPL": 1},
        "sync_short_interest_postgres": {"AAPL": 4},
    }

    for name, result in steps.items():

        async def step(*args, _result=result, _name=name, **kwargs):
            if _name == failing:
                raise RuntimeError(f"{_name} upstream is down")
            return _result

        monkeypatch.setattr(fetch_mod, name, step)


async def test_daily_refresh_records_a_fetch_audit_row(pg_conn, monkeypatch) -> None:
    _stub_refresh_steps(monkeypatch, pg_conn)

    await fetch_mod._run("2026-08-04")

    runs = await list_recent_runs(pg_conn, job_type="fetch", limit=5)
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["error"] is None
    assert runs[0]["counters"] == {
        "prices": {"fetched": 2, "skipped_unresolved": 1},
        "benchmarks": {"fetched": 1, "current": 0, "no_data": 0, "failed": 1},
        "fx": {"USD": 3},
        "volatility_rows": 7,
        "macro": {"DGS10": 2},
        "dividends": {"AAPL": 1},
        "short_interest": {"AAPL": 4},
    }


async def test_daily_refresh_records_partial_counters_when_a_step_fails(
    pg_conn, monkeypatch
) -> None:
    """A run that dies mid-way must still show which sources completed."""
    _stub_refresh_steps(monkeypatch, pg_conn, failing="sync_dividends")

    with pytest.raises(RuntimeError):
        await fetch_mod._run("2026-08-04")

    runs = await list_recent_runs(pg_conn, job_type="fetch", limit=5)
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "sync_dividends upstream is down" in runs[0]["error"]
    counters = runs[0]["counters"]
    assert counters["prices"] == {"fetched": 2, "skipped_unresolved": 1}
    assert counters["macro"] == {"DGS10": 2}
    assert "dividends" not in counters
    assert "short_interest" not in counters
