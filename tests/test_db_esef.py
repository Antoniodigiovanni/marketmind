"""Tests for the ESEF filings repository."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest

from data.db.companies import create_company
from data.db.esef import create_esef_filing, esef_filing_exists, get_european_filings

pytestmark = pytest.mark.asyncio


async def _company(pg_conn, name: str = "TestCo") -> uuid.UUID:
    return await create_company(
        pg_conn, name=name, country="IT", sector="Financials", first_seen=dt.date(2000, 1, 1)
    )


def _kwargs(**overrides: object) -> dict[str, object]:
    base = {
        "fxo_id": "ESEF/IT/1",
        "lei": "815600AB12CD34EF5678",
        "company_id": None,
        "entity_name": "Issuer",
        "country": "IT",
        "filing_system": "ESEF",
        "filed_at": dt.date(2022, 1, 10),
        "period_end": dt.date(2020, 12, 31),
        "primary_document": "ESEF_IT_1.zip",
        "package_url": "https://x/1.zip",
        "xhtml_url": None,
        "json_url": None,
        "text_path": "filings-eu/815600AB12CD34EF5678/ESEF_IT_1.zip",
        "text_hash": "deadbeef",
        "size_bytes": 10,
    }
    base.update(overrides)
    return base


async def test_create_esef_filing_returns_a_uuid(pg_conn) -> None:
    filing_id = await create_esef_filing(pg_conn, **_kwargs())
    assert isinstance(filing_id, uuid.UUID)


async def test_create_esef_filing_rejects_short_lei(pg_conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        await create_esef_filing(pg_conn, **_kwargs(lei="TOO-SHORT"))


async def test_create_esef_filing_rejects_duplicate_fxo_lei(pg_conn) -> None:
    await create_esef_filing(pg_conn, **_kwargs())
    with pytest.raises(psycopg.errors.UniqueViolation):
        await create_esef_filing(pg_conn, **_kwargs())


async def test_esef_filing_exists(pg_conn) -> None:
    assert (
        await esef_filing_exists(pg_conn, fxo_id="ESEF/IT/1", lei="815600AB12CD34EF5678") is False
    )
    await create_esef_filing(pg_conn, **_kwargs())
    assert await esef_filing_exists(pg_conn, fxo_id="ESEF/IT/1", lei="815600AB12CD34EF5678") is True


async def test_get_european_filings_filters(pg_conn) -> None:
    company_id = await _company(pg_conn)
    await create_esef_filing(pg_conn, **_kwargs(company_id=company_id))
    await create_esef_filing(
        pg_conn,
        **_kwargs(
            fxo_id="ESEF/FR/1",
            lei="969500Y00000000AB123",
            country="FR",
            filed_at=dt.date(2023, 2, 1),
        ),
    )

    by_country = await get_european_filings(pg_conn, country="IT")
    assert [r["fxo_id"] for r in by_country] == ["ESEF/IT/1"]

    by_company = await get_european_filings(pg_conn, company_id=company_id)
    assert [r["fxo_id"] for r in by_company] == ["ESEF/IT/1"]

    by_lei = await get_european_filings(pg_conn, lei="969500Y00000000AB123")
    assert [r["fxo_id"] for r in by_lei] == ["ESEF/FR/1"]

    windowed = await get_european_filings(pg_conn, filed_to=dt.date(2022, 12, 31))
    assert [r["fxo_id"] for r in windowed] == ["ESEF/IT/1"]

    by_system = await get_european_filings(pg_conn, filing_system="ESEF")
    assert len(by_system) == 2
    assert await get_european_filings(pg_conn, filing_system="UKSEF") == []

    assert len(await get_european_filings(pg_conn)) == 2


async def test_get_european_filings_empty(pg_conn) -> None:
    assert await get_european_filings(pg_conn) == []
    assert await get_european_filings(pg_conn, country="ZZ") == []
