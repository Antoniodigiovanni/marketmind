"""Tests for the short interest sync job (mocked source, tmp_path storage)."""

from __future__ import annotations

import datetime as dt

import polars as pl

import data.short_interest.fetch as si_mod
from data.db.identifiers import create_identifier
from data.db.listings import upsert_listing
from data.db.market_data import get_short_interest
from data.short_interest.store import settlement_exists, write_settlement
from data.sources.finra import COLUMNS


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


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=COLUMNS)


def test_sync_short_interest_ingests_new_dates(tmp_path, monkeypatch) -> None:
    def fake_fetch(date: dt.date) -> pl.DataFrame:
        return _frame() if date == dt.date(2026, 7, 15) else _empty()

    monkeypatch.setattr(si_mod, "fetch_short_interest_file", fake_fetch)

    summary = si_mod.sync_short_interest(as_of="2026-07-31", si_dir=tmp_path)

    assert summary == {"2026-07-15": 1}
    assert settlement_exists(dt.date(2026, 7, 15), tmp_path) is True


def test_sync_short_interest_skips_already_stored(tmp_path, monkeypatch) -> None:
    write_settlement(dt.date(2026, 7, 15), _frame(), tmp_path)
    fetched: list[dt.date] = []

    def fake_fetch(date: dt.date) -> pl.DataFrame:
        fetched.append(date)
        return _frame() if date == dt.date(2026, 7, 15) else _empty()

    monkeypatch.setattr(si_mod, "fetch_short_interest_file", fake_fetch)

    summary = si_mod.sync_short_interest(as_of="2026-07-31", si_dir=tmp_path)

    assert summary == {}
    assert dt.date(2026, 7, 15) not in fetched


def test_sync_short_interest_probes_window(tmp_path, monkeypatch) -> None:
    probed: list[dt.date] = []

    def fake_fetch(date: dt.date) -> pl.DataFrame:
        probed.append(date)
        return _empty()

    monkeypatch.setattr(si_mod, "fetch_short_interest_file", fake_fetch)

    si_mod.sync_short_interest(as_of="2026-07-31", si_dir=tmp_path)

    assert min(probed) == dt.date(2026, 7, 11)
    assert max(probed) == dt.date(2026, 7, 31)
    assert len(probed) == 21


def test_fetch_main_includes_short_interest(monkeypatch) -> None:
    import data.fetch as fetch_entry

    called: list[str] = []

    async def fake_run(as_of: str) -> None:
        called.append("market_data")

    monkeypatch.setattr(fetch_entry, "_run", fake_run)
    monkeypatch.setattr("sys.argv", ["data/fetch.py", "--as-of", "2026-07-31"])

    fetch_entry.main()

    assert called == ["market_data"]


async def test_postgres_sync_resolves_listing_and_writes_rows(
    pg_conn, tmp_path, monkeypatch
) -> None:
    """A downloaded settlement is archived and inserted for known listings."""
    company_id = await create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="A",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Agilent Technologies",
        country="US",
        sector="Healthcare",
    )
    listing_id = await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol="A",
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )
    monkeypatch.setattr(si_mod, "PROBE_DAYS", 0)
    monkeypatch.setattr(si_mod, "fetch_short_interest_file", lambda _date: _frame())

    summary = await si_mod.sync_short_interest_postgres(
        pg_conn, as_of="2026-07-15", si_dir=tmp_path
    )

    assert summary == {"2026-07-15": 1}
    rows = await get_short_interest(pg_conn, listing_id=listing_id)
    assert rows[0]["current_short"] == 7_538_437.0
