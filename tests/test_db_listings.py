"""Tests for the listings repository."""

from __future__ import annotations

import datetime as dt

import pytest

from data.db.identifiers import get_or_create_identifier
from data.db.listings import get_current_listing, upsert_listing

pytestmark = pytest.mark.asyncio


async def _company(pg_conn, ticker: str = "ASML") -> object:
    return await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value=ticker,
        valid_from=dt.date(2000, 1, 1),
        source="universe-csv",
        name="ASML Holding",
        country="NL",
        sector="Semiconductors",
    )


async def test_upsert_listing_creates_a_new_listing(pg_conn) -> None:
    company_id = await _company(pg_conn)
    listing_id = await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol="ASML.AS",
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )
    row = await get_current_listing(pg_conn, provider="yahoo", symbol="ASML.AS")
    assert row == {"listing_id": listing_id, "company_id": company_id, "status": "valid"}


async def test_upsert_listing_refreshes_status_for_the_same_company(pg_conn) -> None:
    company_id = await _company(pg_conn)
    first_id = await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol="ASML.AS",
        status="unresolved",
        valid_from=dt.date(2000, 1, 1),
    )
    second_id = await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol="ASML.AS",
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )
    assert first_id == second_id
    row = await get_current_listing(pg_conn, provider="yahoo", symbol="ASML.AS")
    assert row["status"] == "valid"


async def test_upsert_listing_raises_on_symbol_collision_with_another_company(pg_conn) -> None:
    company_a = await _company(pg_conn, "ASML")
    company_b = await _company(pg_conn, "OTHER")
    await upsert_listing(
        pg_conn,
        company_id=company_a,
        provider="yahoo",
        symbol="SHARED.AS",
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )
    with pytest.raises(ValueError, match="already live"):
        await upsert_listing(
            pg_conn,
            company_id=company_b,
            provider="yahoo",
            symbol="SHARED.AS",
            status="valid",
            valid_from=dt.date(2000, 1, 1),
        )


async def test_get_current_listing_returns_none_when_unknown(pg_conn) -> None:
    assert await get_current_listing(pg_conn, provider="yahoo", symbol="NOPE") is None
