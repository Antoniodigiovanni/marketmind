"""Tests for the PostgreSQL-backed filings MCP tools."""

from __future__ import annotations

import datetime as dt
import uuid

import polars as pl
import pytest

import server.mcp as mcp_mod
from data.db.esef import create_esef_filing
from data.db.filings import (
    create_filing,
    insert_13f_holdings,
    insert_filing_text,
    insert_insider_rows,
)
from data.db.identifiers import get_or_create_identifier
from data.filings.parse.ownership import InsiderHolding, InsiderTransaction

pytestmark = pytest.mark.asyncio


class _FakePool:
    """A fake pool that hands back the test's own pg_conn.

    The tool code checks out a connection via `pool.connection()`; reusing
    the test's own connection (rather than a real second checkout from a
    pool) means writes made in the test body -- uncommitted, rolled back
    at teardown by the pg_conn fixture -- are visible to the tool under
    test on the same physical connection/transaction.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def connection(self) -> _FakePool:
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patch_pool(monkeypatch: pytest.MonkeyPatch, pg_conn) -> None:
    async def _fake_pg_pool():
        return _FakePool(pg_conn)

    monkeypatch.setattr(mcp_mod, "_pg_pool", _fake_pg_pool)


async def _company_with_ticker(pg_conn, ticker: str, name: str = "TestCo") -> uuid.UUID:
    return await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value=ticker,
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name=name,
        country="US",
        sector="Technology",
    )


async def test_get_filings_filters_and_orders(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    company_id = await _company_with_ticker(pg_conn, "AAPL")
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

    rows = await mcp_mod.get_filings("AAPL", form_types=["8-K"])
    assert len(rows) == 1
    assert rows[0]["form_type"] == "8-K"
    assert rows[0]["filed_at"] == "2026-08-03"
    assert rows[0]["company_id"] == str(company_id)

    window = await mcp_mod.get_filings("AAPL", filed_from="2026-08-01", filed_to="2026-08-03")
    assert [r["accession"] for r in window] == ["a1"]

    all_rows = await mcp_mod.get_filings("AAPL")
    assert [r["accession"] for r in all_rows] == ["a1", "a2"]


async def test_get_filings_empty_for_unresolved_ticker(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    assert await mcp_mod.get_filings("NOPE") == []


async def test_search_filings_returns_snippets(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    company_id = await _company_with_ticker(pg_conn, "AAPL")
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

    hits = await mcp_mod.search_filings(
        "competition", ticker="AAPL", form_types=["10-K"], filed_from="2026-01-01"
    )
    assert len(hits) == 1
    assert hits[0]["company_id"] == str(company_id)
    assert hits[0]["form_type"] == "10-K"
    assert "competition" in hits[0]["snippet"].lower()

    multi = await mcp_mod.search_filings("competition phones")
    assert len(multi) == 1
    assert await mcp_mod.search_filings("competition bonds") == []


async def test_search_filings_empty_and_blank_query(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    assert await mcp_mod.search_filings("anything") == []
    assert await mcp_mod.search_filings("   ") == []


async def test_search_filings_unresolved_ticker(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    assert await mcp_mod.search_filings("anything", ticker="NOPE") == []


async def test_get_european_filings_filters_and_orders(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    company_it = await _company_with_ticker(pg_conn, "BAMI.MI", "Banco BPM")
    await create_esef_filing(
        pg_conn,
        fxo_id="ESEF/IT/1",
        lei="815600AB12CD34EF5678",
        company_id=company_it,
        entity_name="Issuer",
        country="IT",
        filing_system="ESEF",
        filed_at=dt.date(2022, 1, 10),
        period_end=None,
        primary_document=None,
        package_url="https://x/1.zip",
        xhtml_url=None,
        json_url=None,
        text_path=None,
        text_hash=None,
        size_bytes=None,
    )
    company_fr = await _company_with_ticker(pg_conn, "SOPR.PA", "Sopra Steria")
    await create_esef_filing(
        pg_conn,
        fxo_id="ESEF/FR/1",
        lei="969500Y00000000AB123",
        company_id=company_fr,
        entity_name="Issuer",
        country="FR",
        filing_system="ESEF",
        filed_at=dt.date(2023, 2, 1),
        period_end=None,
        primary_document=None,
        package_url="https://x/2.zip",
        xhtml_url=None,
        json_url=None,
        text_path=None,
        text_hash=None,
        size_bytes=None,
    )

    by_country = await mcp_mod.get_european_filings(country="IT")
    assert [r["fxo_id"] for r in by_country] == ["ESEF/IT/1"]

    by_ticker = await mcp_mod.get_european_filings(ticker="SOPR.PA")
    assert by_ticker[0]["country"] == "FR"

    by_lei = await mcp_mod.get_european_filings(lei="815600AB12CD34EF5678")
    assert by_lei[0]["company_id"] == str(company_it)

    window = await mcp_mod.get_european_filings(as_of="2022-12-31")
    assert [r["fxo_id"] for r in window] == ["ESEF/IT/1"]


async def test_get_european_filings_empty(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    assert await mcp_mod.get_european_filings() == []
    assert await mcp_mod.get_european_filings(ticker="NOPE") == []


async def test_ownership_and_13f_tools(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    company_id = await _company_with_ticker(pg_conn, "AAPL")
    filing_id = await create_filing(
        pg_conn,
        company_id=company_id,
        accession="own-1",
        form_type="4",
        filed_at=dt.date(2026, 5, 2),
        period=dt.date(2026, 4, 30),
        url="https://example.com/own",
    )
    await insert_insider_rows(
        pg_conn,
        filing_id=filing_id,
        company_id=company_id,
        transactions=[
            InsiderTransaction(
                "AAPL",
                "TIM COOK",
                None,
                "CEO",
                "Common",
                "2026-04-30",
                None,
                "S",
                500,
                180,
                9500,
                "D",
            )
        ],
        holdings=[
            InsiderHolding("AAPL", "TIM COOK", None, "CEO", "Common", "2026-04-30", 9500, "D", "4")
        ],
    )
    assert (await mcp_mod.get_insider_transactions("AAPL"))[0]["owner_name"] == "TIM COOK"
    assert (await mcp_mod.get_insider_holdings("AAPL"))[0]["shares_held_after"] == 9500

    filing_13f = await create_filing(
        pg_conn,
        company_id=None,
        accession="13f-tool",
        form_type="13F-HR",
        filed_at=dt.date(2026, 5, 15),
        period=dt.date(2026, 3, 31),
        url="https://example.com/13f",
    )
    await insert_13f_holdings(
        pg_conn,
        filing_id=filing_13f,
        frame=pl.DataFrame(
            {
                "filer_cik": ["0001"],
                "filer_name": ["Fund"],
                "cusip": ["037833100"],
                "report_period": ["2026-03-31"],
                "issuer_name": ["APPLE INC"],
                "class_title": ["COM"],
                "shares": [10.0],
                "value_usd": [100000.0],
                "put_call": [None],
            }
        ),
    )
    assert (await mcp_mod.get_13f_holdings("037833100"))[0]["filer_name"] == "Fund"
