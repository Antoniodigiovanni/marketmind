"""Tests for the companies repository."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from data.db.companies import (
    create_company,
    get_company,
    update_company_attributes,
    update_company_description,
)

pytestmark = pytest.mark.asyncio


async def test_create_company_returns_a_uuid(pg_conn) -> None:
    company_id = await create_company(
        pg_conn,
        name="Apple Inc.",
        country="US",
        sector="Technology",
        first_seen=dt.date(1990, 1, 1),
    )
    assert isinstance(company_id, uuid.UUID)


async def test_get_company_returns_what_was_created(pg_conn) -> None:
    company_id = await create_company(
        pg_conn,
        name="ASML Holding",
        country="NL",
        sector="Semiconductors",
        first_seen=dt.date(2000, 1, 1),
    )
    row = await get_company(pg_conn, company_id=company_id)
    assert row["name"] == "ASML Holding"
    assert row["country"] == "NL"
    assert row["sector"] == "Semiconductors"
    assert row["first_seen"] == dt.date(2000, 1, 1)


async def test_get_company_returns_none_for_unknown_id(pg_conn) -> None:
    row = await get_company(pg_conn, company_id=uuid.uuid4())
    assert row is None


async def test_update_company_attributes_changes_the_row(pg_conn) -> None:
    company_id = await create_company(
        pg_conn,
        name="Old Name",
        country="US",
        sector="Old Sector",
        first_seen=dt.date(2000, 1, 1),
    )
    await update_company_attributes(
        pg_conn,
        company_id=company_id,
        name="New Name",
        country="GB",
        sector="New Sector",
    )
    row = await get_company(pg_conn, company_id=company_id)
    assert row["name"] == "New Name"
    assert row["country"] == "GB"
    assert row["sector"] == "New Sector"


async def test_update_company_attributes_preserves_country_sector_on_none(pg_conn) -> None:
    company_id = await create_company(
        pg_conn,
        name="Old Name",
        country="US",
        sector="Old Sector",
        first_seen=dt.date(2000, 1, 1),
    )
    await update_company_attributes(
        pg_conn,
        company_id=company_id,
        name="New Name",
        country=None,
        sector=None,
    )
    row = await get_company(pg_conn, company_id=company_id)
    assert row["name"] == "New Name"
    assert row["country"] == "US"
    assert row["sector"] == "Old Sector"


async def test_update_company_attributes_bumps_updated_at(pg_conn) -> None:
    company_id = await create_company(
        pg_conn,
        name="X",
        country=None,
        sector=None,
        first_seen=dt.date(2000, 1, 1),
    )
    before = await (
        await pg_conn.execute(
            "SELECT updated_at FROM companies WHERE company_id = %s", (company_id,)
        )
    ).fetchone()
    await update_company_attributes(
        pg_conn,
        company_id=company_id,
        name="X",
        country=None,
        sector=None,
    )
    after = await (
        await pg_conn.execute(
            "SELECT updated_at FROM companies WHERE company_id = %s", (company_id,)
        )
    ).fetchone()
    assert after[0] >= before[0]


async def test_update_company_description_overwrites_unconditionally(pg_conn) -> None:
    company_id = await create_company(
        pg_conn, name="X", country=None, sector=None, first_seen=dt.date(2000, 1, 1)
    )
    await update_company_description(
        pg_conn, company_id=company_id, description="First.", description_source="wikipedia"
    )
    row = await get_company(pg_conn, company_id=company_id)
    assert row["description"] == "First."
    assert row["description_source"] == "wikipedia"

    await update_company_description(
        pg_conn, company_id=company_id, description="Second.", description_source=None
    )
    row = await get_company(pg_conn, company_id=company_id)
    assert row["description"] == "Second."
    assert row["description_source"] is None
