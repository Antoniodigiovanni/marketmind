"""Tests for identifier resolution and identity get-or-create."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from data.db.companies import get_company
from data.db.identifiers import (
    get_current_company_id,
    get_or_create_identifier,
    resolve_identifier,
)

pytestmark = pytest.mark.asyncio


async def test_get_or_create_identifier_creates_a_new_company(pg_conn) -> None:
    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(1990, 1, 1),
        source="universe-csv",
        name="Apple Inc.",
        country="US",
        sector="Technology",
    )
    row = await get_company(pg_conn, company_id=company_id)
    assert row["name"] == "Apple Inc."


async def test_get_or_create_identifier_is_idempotent(pg_conn) -> None:
    first = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(1990, 1, 1),
        source="universe-csv",
        name="Apple Inc.",
        country="US",
        sector="Technology",
    )
    second = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(1990, 1, 1),
        source="universe-csv",
        name="Apple Inc.",
        country="US",
        sector="Technology",
    )
    assert first == second


async def test_get_or_create_identifier_refreshes_existing_company_attributes(pg_conn) -> None:
    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(1990, 1, 1),
        source="universe-csv",
        name="Apple Inc.",
        country="US",
        sector="Technology",
    )
    again = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(1990, 1, 1),
        source="universe-csv",
        name="Apple Incorporated",
        country="US",
        sector="Tech",
    )
    assert again == company_id
    row = await get_company(pg_conn, company_id=company_id)
    assert row["name"] == "Apple Incorporated"
    assert row["sector"] == "Tech"


async def test_get_current_company_id_returns_none_when_unknown(pg_conn) -> None:
    assert await get_current_company_id(pg_conn, id_type="ticker", id_value="NOPE") is None


async def test_resolve_identifier_is_point_in_time(pg_conn) -> None:
    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="ASML",
        valid_from=dt.date(2000, 1, 1),
        source="universe-csv",
        name="ASML Holding",
        country="NL",
        sector="Semiconductors",
    )
    assert (
        await resolve_identifier(
            pg_conn, id_type="ticker", id_value="ASML", as_of=dt.date(2000, 1, 1)
        )
        == company_id
    )
    assert (
        await resolve_identifier(
            pg_conn, id_type="ticker", id_value="ASML", as_of=dt.date(1999, 12, 31)
        )
        is None
    )


async def test_get_or_create_identifier_is_atomic_on_failure(pg_conn) -> None:
    """If the identifier insert fails after the company insert succeeds
    (simulated here with a bad id_type), neither row should survive --
    company creation and its identifier are one atomic unit, not two."""
    with pytest.raises(psycopg.errors.CheckViolation):
        await get_or_create_identifier(
            pg_conn,
            id_type="not-a-real-type",
            id_value="X",
            valid_from=dt.date(2000, 1, 1),
            source="universe-csv",
            name="Should Not Persist",
            country=None,
            sector=None,
        )
    row = await (
        await pg_conn.execute("SELECT 1 FROM companies WHERE name = 'Should Not Persist'")
    ).fetchone()
    assert row is None
