"""Dividend fetch-to-PostgreSQL tests."""

from __future__ import annotations

import datetime as dt

import polars as pl

import data.dividends.fetch as dividends
from data.db.identifiers import get_or_create_identifier
from data.db.listings import upsert_listing


async def test_sync_dividends_archives_and_upserts(pg_conn, tmp_path, monkeypatch) -> None:
    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )
    await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol="AAPL",
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )
    frame = pl.DataFrame({"ex_date": [dt.date(2026, 5, 15)], "amount": [0.26]})
    monkeypatch.setattr(dividends, "fetch_dividends", lambda *args: frame)
    members = pl.DataFrame(
        {
            "ticker": ["AAPL"],
            "name": ["Apple"],
            "country": ["US"],
            "sector": ["Technology"],
            "added_date": ["2000-01-01"],
            "removed_date": [None],
        }
    )
    result = await dividends.sync_dividends(
        pg_conn, as_of="2026-08-31", dividends_dir=tmp_path, members=members
    )
    assert result == {"AAPL": 1}
    assert (tmp_path / "AAPL.parquet").exists()
    count = await (await pg_conn.execute("SELECT COUNT(*) FROM dividends")).fetchone()
    assert count[0] == 1
