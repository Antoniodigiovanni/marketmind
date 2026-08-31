"""Tests for the filings repository (filings, filing_text, fetch_checkpoints)."""

from __future__ import annotations

import datetime as dt
import uuid

import polars as pl
import psycopg
import pytest

from data.db.companies import create_company
from data.db.filings import (
    create_filing,
    filing_exists,
    get_13f_holdings,
    get_checkpoint_date,
    get_filings,
    get_insider_holdings,
    get_insider_transactions,
    has_checkpoint,
    insert_13f_holdings,
    insert_filing_text,
    insert_insider_rows,
    mark_checkpoint,
    search_filing_text,
)
from data.filings.parse.ownership import InsiderHolding, InsiderTransaction

pytestmark = pytest.mark.asyncio


async def _company(pg_conn, name: str = "TestCo") -> uuid.UUID:
    return await create_company(
        pg_conn, name=name, country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )


async def test_create_filing_returns_a_uuid(pg_conn) -> None:
    company_id = await _company(pg_conn)
    filing_id = await create_filing(
        pg_conn,
        company_id=company_id,
        accession="000032019326000045",
        form_type="8-K",
        filed_at=dt.date(2026, 8, 3),
        period=dt.date(2026, 8, 3),
        url="https://www.sec.gov/Archives/edgar/data/x/form8k.htm",
    )
    assert isinstance(filing_id, uuid.UUID)


async def test_create_filing_allows_null_company_id(pg_conn) -> None:
    filing_id = await create_filing(
        pg_conn,
        company_id=None,
        accession="000032019326000046",
        form_type="8-K",
        filed_at=dt.date(2026, 8, 3),
        period=None,
        url="https://example.com",
    )
    assert isinstance(filing_id, uuid.UUID)


async def test_create_filing_rejects_duplicate_accession(pg_conn) -> None:
    company_id = await _company(pg_conn)
    await create_filing(
        pg_conn,
        company_id=company_id,
        accession="dupe-accession",
        form_type="8-K",
        filed_at=dt.date(2026, 8, 3),
        period=None,
        url="https://example.com/1",
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        await create_filing(
            pg_conn,
            company_id=company_id,
            accession="dupe-accession",
            form_type="10-Q",
            filed_at=dt.date(2026, 8, 4),
            period=None,
            url="https://example.com/2",
        )


async def test_filing_exists(pg_conn) -> None:
    company_id = await _company(pg_conn)
    assert await filing_exists(pg_conn, accession="acc-1") is False
    await create_filing(
        pg_conn,
        company_id=company_id,
        accession="acc-1",
        form_type="8-K",
        filed_at=dt.date(2026, 8, 3),
        period=None,
        url="https://example.com",
    )
    assert await filing_exists(pg_conn, accession="acc-1") is True


async def test_get_filings_filters_by_form_type_and_date_window(pg_conn) -> None:
    company_id = await _company(pg_conn)
    await create_filing(
        pg_conn,
        company_id=company_id,
        accession="a1",
        form_type="8-K",
        filed_at=dt.date(2026, 8, 3),
        period=None,
        url="https://example.com/a1",
    )
    await create_filing(
        pg_conn,
        company_id=company_id,
        accession="a2",
        form_type="10-Q",
        filed_at=dt.date(2026, 7, 30),
        period=dt.date(2026, 6, 27),
        url="https://example.com/a2",
    )

    only_8k = await get_filings(pg_conn, company_id=company_id, form_types=["8-K"])
    assert [r["accession"] for r in only_8k] == ["a1"]

    windowed = await get_filings(
        pg_conn,
        company_id=company_id,
        filed_from=dt.date(2026, 8, 1),
        filed_to=dt.date(2026, 8, 3),
    )
    assert [r["accession"] for r in windowed] == ["a1"]

    all_rows = await get_filings(pg_conn, company_id=company_id)
    assert [r["accession"] for r in all_rows] == ["a1", "a2"]


async def test_get_filings_scoped_to_company(pg_conn) -> None:
    company_a = await _company(pg_conn, "CompanyA")
    company_b = await _company(pg_conn, "CompanyB")
    await create_filing(
        pg_conn,
        company_id=company_b,
        accession="b1",
        form_type="8-K",
        filed_at=dt.date(2026, 8, 3),
        period=None,
        url="https://example.com/b1",
    )
    assert await get_filings(pg_conn, company_id=company_a) == []


async def test_insert_filing_text_and_search(pg_conn) -> None:
    company_id = await _company(pg_conn)
    filing_id = await create_filing(
        pg_conn,
        company_id=company_id,
        accession="a1",
        form_type="10-K",
        filed_at=dt.date(2026, 1, 15),
        period=None,
        url="https://example.com/a1",
    )
    await insert_filing_text(
        pg_conn, filing_id=filing_id, body="Apple faces intense competition in phones."
    )

    hits = await search_filing_text(pg_conn, query="competition", company_id=company_id)
    assert len(hits) == 1
    assert hits[0]["filing_id"] == filing_id
    assert "competition" in hits[0]["snippet"].lower()

    multi = await search_filing_text(pg_conn, query="competition phones")
    assert len(multi) == 1

    assert await search_filing_text(pg_conn, query="competition bonds") == []
    assert await search_filing_text(pg_conn, query="   ") == []


async def test_search_filing_text_filters_by_form_type_and_date(pg_conn) -> None:
    company_id = await _company(pg_conn)
    filing_id = await create_filing(
        pg_conn,
        company_id=company_id,
        accession="a1",
        form_type="10-K",
        filed_at=dt.date(2026, 1, 15),
        period=None,
        url="https://example.com/a1",
    )
    await insert_filing_text(pg_conn, filing_id=filing_id, body="Risk factors include inflation.")

    assert len(await search_filing_text(pg_conn, query="inflation", form_types=["10-K"])) == 1
    assert await search_filing_text(pg_conn, query="inflation", form_types=["8-K"]) == []
    assert (
        len(await search_filing_text(pg_conn, query="inflation", filed_from=dt.date(2026, 1, 1)))
        == 1
    )
    assert (
        await search_filing_text(pg_conn, query="inflation", filed_from=dt.date(2026, 2, 1)) == []
    )


async def test_checkpoint_round_trip(pg_conn) -> None:
    assert await has_checkpoint(pg_conn, source="submissions:AAPL", key="2026-08-05") is False
    await mark_checkpoint(pg_conn, source="submissions:AAPL", key="2026-08-05")
    assert await has_checkpoint(pg_conn, source="submissions:AAPL", key="2026-08-05") is True


async def test_mark_checkpoint_is_idempotent(pg_conn) -> None:
    await mark_checkpoint(pg_conn, source="13f", key="ACC1", last_date=dt.date(2026, 1, 1))
    await mark_checkpoint(pg_conn, source="13f", key="ACC1", last_date=dt.date(2026, 2, 1))
    row = await (
        await pg_conn.execute(
            "SELECT last_date FROM fetch_checkpoints WHERE source = %s AND key = %s",
            ("13f", "ACC1"),
        )
    ).fetchone()
    assert row[0] == dt.date(2026, 2, 1)
    assert await get_checkpoint_date(pg_conn, source="13f", key="ACC1") == dt.date(2026, 2, 1)
    assert await get_checkpoint_date(pg_conn, source="13f", key="missing") is None


async def test_ownership_and_13f_round_trip(pg_conn) -> None:
    company_id = await _company(pg_conn)
    filing_id = await create_filing(
        pg_conn,
        company_id=company_id,
        accession="ownership-1",
        form_type="4",
        filed_at=dt.date(2026, 5, 2),
        period=dt.date(2026, 4, 30),
        url="https://example.com/ownership",
    )
    await insert_insider_rows(
        pg_conn,
        filing_id=filing_id,
        company_id=company_id,
        transactions=[
            InsiderTransaction(
                ticker="AAPL",
                owner_name="TIM COOK",
                owner_cik=None,
                owner_title="CEO",
                security_title="Common Stock",
                transaction_date="2026-04-30",
                deemed_execution_date=None,
                transaction_code="S",
                shares_transacted=500,
                price_per_share=180.25,
                shares_held_after=9500,
                direct_or_indirect="D",
            )
        ],
        holdings=[
            InsiderHolding(
                ticker="AAPL",
                owner_name="TIM COOK",
                owner_cik=None,
                owner_title="CEO",
                security_title="Common Stock",
                ref_date="2026-04-30",
                shares_held_after=9500,
                direct_or_indirect="D",
                form_type="4",
            )
        ],
    )
    transactions = await get_insider_transactions(pg_conn, company_id=company_id)
    holdings = await get_insider_holdings(pg_conn, company_id=company_id)
    assert transactions[0]["shares_transacted"] == 500
    assert holdings[0]["shares_held_after"] == 9500

    filing_13f = await create_filing(
        pg_conn,
        company_id=None,
        accession="13f-1",
        form_type="13F-HR",
        filed_at=dt.date(2026, 5, 15),
        period=dt.date(2026, 3, 31),
        url="https://example.com/13f",
    )
    frame = pl.DataFrame(
        {
            "filer_cik": ["0001"],
            "filer_name": ["Fund"],
            "cusip": ["037833100"],
            "report_period": ["2026-03-31"],
            "issuer_name": ["APPLE INC"],
            "class_title": ["COM"],
            "shares": [10.0],
            "value_usd": [100_000.0],
            "put_call": [None],
        }
    )
    assert await insert_13f_holdings(pg_conn, filing_id=filing_13f, frame=frame) == 1
    rows = await get_13f_holdings(pg_conn, cusip="037833100")
    assert rows[0]["filer_name"] == "Fund"
