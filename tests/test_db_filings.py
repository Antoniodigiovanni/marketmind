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


async def test_insider_holdings_publication_cutoff(pg_conn) -> None:
    company_id = await _company(pg_conn)
    for accession, form, published, event, shares in [
        ("original", "3", "2026-05-02", "2026-04-30", 100),
        ("amendment", "3/A", "2026-05-10", "2026-04-30", 110),
        ("future-event", "3", "2026-05-01", "2026-06-01", 200),
        ("older", "3", "2026-04-02", "2026-04-01", 50),
    ]:
        filing_id = await create_filing(
            pg_conn,
            company_id=company_id,
            accession=accession,
            form_type=form,
            filed_at=dt.date.fromisoformat(published),
            period=dt.date.fromisoformat(event),
            url=f"https://example.com/{accession}",
        )
        await insert_insider_rows(
            pg_conn,
            filing_id=filing_id,
            company_id=company_id,
            transactions=[],
            holdings=[
                InsiderHolding("TEST", "Owner", None, None, "Common", event, shares, "D", form)
            ],
        )

    async def at(day: str) -> list[dict[str, object]]:
        return await get_insider_holdings(
            pg_conn, company_id=company_id, as_of=dt.date.fromisoformat(day)
        )

    assert [r["shares_held_after"] for r in await at("2026-05-01")] == [50]
    assert [r["shares_held_after"] for r in await at("2026-05-02")] == [100, 50]
    assert [r["shares_held_after"] for r in await at("2026-05-09")] == [100, 50]
    visible = await at("2026-05-10")
    assert {r["form_type"] for r in visible[:2]} == {"3", "3/A"}
    assert visible[-1]["shares_held_after"] == 50
    assert set(visible[0]) == {
        "id",
        "owner_name",
        "owner_title",
        "security_title",
        "ref_date",
        "shares_held_after",
        "direct_or_indirect",
        "form_type",
    }
    limited = await get_insider_holdings(
        pg_conn, company_id=company_id, as_of=dt.date(2026, 5, 2), limit=1
    )
    assert [r["shares_held_after"] for r in limited] == [100]
    unrestricted = await get_insider_holdings(pg_conn, company_id=company_id)
    assert len(unrestricted) == 4
    assert unrestricted[0]["shares_held_after"] == 200


async def test_13f_holdings_publication_cutoff(pg_conn) -> None:
    for accession, form, published, period, value in [
        ("original", "13F-HR", "2026-05-15", "2026-03-31", 100_000),
        ("amendment", "13F-HR/A", "2026-05-20", "2026-03-31", 110_000),
        ("future-period", "13F-HR", "2026-05-01", "2026-06-30", 200_000),
        ("older", "13F-HR", "2026-02-15", "2025-12-31", 150_000),
    ]:
        filing_id = await create_filing(
            pg_conn,
            company_id=None,
            accession=accession,
            form_type=form,
            filed_at=dt.date.fromisoformat(published),
            period=dt.date.fromisoformat(period),
            url=f"https://example.com/{accession}",
        )
        await insert_13f_holdings(
            pg_conn,
            filing_id=filing_id,
            frame=pl.DataFrame(
                {
                    "filer_cik": ["0001"],
                    "filer_name": ["Fund"],
                    "cusip": ["037833100"],
                    "report_period": [period],
                    "issuer_name": ["APPLE INC"],
                    "class_title": ["COM"],
                    "shares": [10.0],
                    "value_usd": [float(value)],
                    "put_call": [None],
                }
            ),
        )

    async def at(day: str, **kwargs: object) -> list[dict[str, object]]:
        return await get_13f_holdings(
            pg_conn, cusip="037833100", as_of=dt.date.fromisoformat(day), **kwargs
        )

    assert [r["value_usd"] for r in await at("2026-05-14")] == [150_000]
    assert [r["value_usd"] for r in await at("2026-05-15")] == [100_000, 150_000]
    assert [r["value_usd"] for r in await at("2026-05-19")] == [100_000, 150_000]
    visible = await at("2026-05-20")
    assert [r["value_usd"] for r in visible] == [110_000, 100_000, 150_000]
    assert [r["filer_cik"] for r in visible[:2]] == ["0001", "0001"]
    assert [r["report_period"] for r in visible[:2]] == [dt.date(2026, 3, 31)] * 2
    assert set(visible[0]) == {
        "filer_name",
        "filer_cik",
        "report_period",
        "issuer_name",
        "class_title",
        "value_usd",
        "shares",
        "put_call",
    }
    assert [r["value_usd"] for r in await at("2026-05-15", limit=1)] == [100_000]
    assert [r["value_usd"] for r in await at("2026-05-20", min_value=110_000)] == [110_000, 150_000]
    assert [r["value_usd"] for r in await at("2026-05-20", min_value=110_000, limit=1)] == [110_000]
    unrestricted = await get_13f_holdings(pg_conn, cusip="037833100")
    assert [r["value_usd"] for r in unrestricted] == [200_000, 110_000, 100_000, 150_000]
    assert await get_13f_holdings(pg_conn, cusip="missing") == []
