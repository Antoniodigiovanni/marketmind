"""Tests for the company profiles repository."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from data.db.companies import create_company
from data.db.company_profiles import create_profile, get_current_profile

pytestmark = pytest.mark.asyncio


async def _company(pg_conn, name: str = "TestCo") -> uuid.UUID:
    return await create_company(
        pg_conn, name=name, country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )


async def test_create_profile_returns_a_uuid(pg_conn) -> None:
    company_id = await _company(pg_conn)
    profile_id = await create_profile(
        pg_conn,
        company_id=company_id,
        description="A technology company.",
        products_services=["Software"],
        valid_from=dt.date(2020, 1, 1),
        generated_by="deepseek-chat",
    )
    assert isinstance(profile_id, uuid.UUID)


async def test_get_current_profile_returns_what_was_created(pg_conn) -> None:
    company_id = await _company(pg_conn)
    await create_profile(
        pg_conn,
        company_id=company_id,
        description="A technology company.",
        products_services=["Software", "Cloud services"],
        valid_from=dt.date(2020, 1, 1),
        generated_by="deepseek-chat",
        website_url="https://example.com",
        sources={"wikipedia": "https://en.wikipedia.org/wiki/TestCo"},
    )
    profile = await get_current_profile(pg_conn, company_id=company_id, as_of=dt.date(2020, 6, 1))
    assert profile["description"] == "A technology company."
    assert profile["products_services"] == ["Software", "Cloud services"]
    assert profile["website_url"] == "https://example.com"
    assert profile["sources"] == {"wikipedia": "https://en.wikipedia.org/wiki/TestCo"}
    assert profile["generated_by"] == "deepseek-chat"


async def test_create_profile_defaults_sources_to_empty_dict(pg_conn) -> None:
    company_id = await _company(pg_conn)
    await create_profile(
        pg_conn,
        company_id=company_id,
        description="A technology company.",
        products_services=["Software"],
        valid_from=dt.date(2020, 1, 1),
        generated_by="deepseek-chat",
    )
    profile = await get_current_profile(pg_conn, company_id=company_id, as_of=dt.date(2020, 6, 1))
    assert profile["sources"] == {}


async def test_get_current_profile_returns_none_for_unknown_company(pg_conn) -> None:
    company_id = await _company(pg_conn)
    profile = await get_current_profile(pg_conn, company_id=company_id, as_of=dt.date(2020, 6, 1))
    assert profile is None


async def test_get_current_profile_is_point_in_time(pg_conn) -> None:
    company_id = await _company(pg_conn)
    await create_profile(
        pg_conn,
        company_id=company_id,
        description="A technology company.",
        products_services=["Software"],
        valid_from=dt.date(2020, 1, 1),
        generated_by="deepseek-chat",
    )
    before = await get_current_profile(pg_conn, company_id=company_id, as_of=dt.date(2019, 12, 31))
    assert before is None


async def test_create_profile_with_supersedes_closes_the_old_profile(pg_conn) -> None:
    company_id = await _company(pg_conn)
    old_id = await create_profile(
        pg_conn,
        company_id=company_id,
        description="Old description.",
        products_services=["Software"],
        valid_from=dt.date(2020, 1, 1),
        generated_by="deepseek-chat",
    )
    new_id = await create_profile(
        pg_conn,
        company_id=company_id,
        description="New description.",
        products_services=["Software", "AI"],
        valid_from=dt.date(2021, 1, 1),
        generated_by="deepseek-chat",
        supersedes_id=old_id,
    )

    before = await get_current_profile(pg_conn, company_id=company_id, as_of=dt.date(2020, 6, 1))
    assert before["profile_id"] == old_id

    after = await get_current_profile(pg_conn, company_id=company_id, as_of=dt.date(2021, 6, 1))
    assert after["profile_id"] == new_id

    row = await (
        await pg_conn.execute(
            "SELECT supersedes_id FROM company_profiles WHERE profile_id = %s", (new_id,)
        )
    ).fetchone()
    assert row[0] == old_id
