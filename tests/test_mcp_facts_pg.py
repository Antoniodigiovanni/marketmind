"""Tests for the Postgres-backed get_facts/save_fact/get_company_profile MCP tools."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

import server.mcp as mcp_mod
from data.db.company_profiles import create_profile
from data.db.identifiers import get_or_create_identifier

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


async def _company(pg_conn, ticker: str = "AAPL") -> uuid.UUID:
    return await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value=ticker,
        valid_from=dt.date(2000, 1, 1),
        source="universe-csv",
        name="Apple Inc.",
        country="US",
        sector="Technology",
    )


async def _article(pg_conn) -> uuid.UUID:
    row = await (
        await pg_conn.execute(
            "INSERT INTO articles (url_hash, content_hash, url, source, published_at) "
            "VALUES (%s, %s, %s, %s, NOW()) RETURNING article_id",
            ("uh", "ch", "http://example.com", "test"),
        )
    ).fetchone()
    return row[0]


async def test_get_facts_returns_empty_for_unresolvable_ticker(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    result = await mcp_mod.get_facts("NOPE", "2020-01-01")
    assert result == []


async def test_save_fact_then_get_facts_round_trip(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    await _company(pg_conn, "AAPL")
    article_id = await _article(pg_conn)

    fact_id = await mcp_mod.save_fact(
        ticker="AAPL",
        statement="Revenue grew 12% YoY",
        fact_type="guidance",
        confidence=0.9,
        valid_from="2020-01-01",
        source_article_id=str(article_id),
    )
    assert fact_id

    facts = await mcp_mod.get_facts("AAPL", "2020-06-01")
    assert len(facts) == 1
    assert facts[0]["statement"] == "Revenue grew 12% YoY"


async def test_get_facts_is_point_in_time(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    await _company(pg_conn, "AAPL")
    article_id = await _article(pg_conn)
    await mcp_mod.save_fact(
        ticker="AAPL",
        statement="Revenue grew 12% YoY",
        fact_type="guidance",
        confidence=0.9,
        valid_from="2020-01-01",
        source_article_id=str(article_id),
    )
    before = await mcp_mod.get_facts("AAPL", "2019-12-31")
    assert before == []


async def test_get_company_profile_returns_none_for_unresolvable_ticker(
    monkeypatch, pg_conn
) -> None:
    _patch_pool(monkeypatch, pg_conn)
    result = await mcp_mod.get_company_profile("NOPE")
    assert result is None


async def test_get_company_profile_returns_profile_and_company_fields(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    company_id = await _company(pg_conn, "AAPL")
    await create_profile(
        pg_conn,
        company_id=company_id,
        description="Makes phones.",
        products_services=["Hardware"],
        valid_from=dt.date(2020, 1, 1),
        generated_by="deepseek-chat",
        website_url="https://www.apple.com",
    )

    profile = await mcp_mod.get_company_profile("AAPL", "2020-06-01")
    assert profile["ticker"] == "AAPL"
    assert profile["name"] == "Apple Inc."
    assert profile["sector"] == "Technology"
    assert profile["country"] == "US"
    assert profile["description"] == "Makes phones."
    assert profile["products_services"] == ["Hardware"]
    assert profile["website_url"] == "https://www.apple.com"
    assert profile["generated_by"] == "deepseek-chat"


async def test_get_company_profile_is_point_in_time(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    company_id = await _company(pg_conn, "AAPL")
    await create_profile(
        pg_conn,
        company_id=company_id,
        description="Makes phones.",
        products_services=["Hardware"],
        valid_from=dt.date(2020, 1, 1),
        generated_by="deepseek-chat",
    )

    assert await mcp_mod.get_company_profile("AAPL", "2019-12-31") is None
    assert await mcp_mod.get_company_profile("AAPL", "2020-06-01") is not None


async def test_get_company_profile_returns_none_when_no_profile_exists(
    monkeypatch, pg_conn
) -> None:
    _patch_pool(monkeypatch, pg_conn)
    await _company(pg_conn, "AAPL")
    result = await mcp_mod.get_company_profile("AAPL", "2020-06-01")
    assert result is None
