"""Tests for the Postgres-backed get_facts/save_fact/get_company_profile MCP tools."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

import server.mcp as mcp_mod
from data.db.companies import create_company
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


async def _reassigned_ticker(pg_conn, ticker: str = "XYZ") -> tuple[uuid.UUID, uuid.UUID]:
    """Simulate a delisted ticker later reassigned to an unrelated company.

    Company A held `ticker` 2010-01-01..2018-01-01 (then delisted); Company
    B holds it 2022-01-01..present. get_or_create_identifier deliberately
    doesn't model reassignment (see its docstring), so the rows are
    inserted directly, the way a real point-in-time universe backfill
    eventually will.
    """
    company_a = await create_company(
        pg_conn, name="Old Co", country="US", sector="Energy", first_seen=dt.date(2010, 1, 1)
    )
    await pg_conn.execute(
        "INSERT INTO company_identifiers "
        "(company_id, id_type, id_value, valid_from, valid_until, is_primary, source) "
        "VALUES (%s, 'ticker', %s, %s, %s, TRUE, 'test')",
        (company_a, ticker, dt.date(2010, 1, 1), dt.date(2018, 1, 1)),
    )
    company_b = await create_company(
        pg_conn, name="New Co", country="US", sector="Technology", first_seen=dt.date(2022, 1, 1)
    )
    await pg_conn.execute(
        "INSERT INTO company_identifiers "
        "(company_id, id_type, id_value, valid_from, is_primary, source) "
        "VALUES (%s, 'ticker', %s, %s, TRUE, 'test')",
        (company_b, ticker, dt.date(2022, 1, 1)),
    )
    return company_a, company_b


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


async def test_save_fact_rejects_an_unresolvable_ticker(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    article_id = await _article(pg_conn)

    with pytest.raises(ValueError, match="does not resolve to a tracked company"):
        await mcp_mod.save_fact(
            ticker="NOPE",
            statement="Unknown company statement",
            fact_type="risk",
            confidence=0.5,
            valid_from="2020-01-01",
            source_article_id=str(article_id),
        )


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


async def test_get_facts_requires_as_of(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    with pytest.raises(ValueError, match="as_of is required"):
        await mcp_mod.get_facts("AAPL", "")


async def test_save_fact_and_get_facts_resolve_ticker_as_of_not_current_holder(
    monkeypatch, pg_conn
) -> None:
    """A ticker reused across two unrelated companies must resolve to
    whichever company held it on the given date, not today's holder --
    the exact scenario CLAUDE.md's identity model exists to handle."""
    _patch_pool(monkeypatch, pg_conn)
    await _reassigned_ticker(pg_conn, "XYZ")
    article_id = await _article(pg_conn)

    await mcp_mod.save_fact(
        ticker="XYZ",
        statement="Old Co statement",
        fact_type="guidance",
        confidence=0.9,
        valid_from="2015-01-01",
        source_article_id=str(article_id),
    )
    await mcp_mod.save_fact(
        ticker="XYZ",
        statement="New Co statement",
        fact_type="guidance",
        confidence=0.9,
        valid_from="2023-01-01",
        source_article_id=str(article_id),
    )

    old_facts = await mcp_mod.get_facts("XYZ", "2015-06-01")
    assert [f["statement"] for f in old_facts] == ["Old Co statement"]

    new_facts = await mcp_mod.get_facts("XYZ", "2023-06-01")
    assert [f["statement"] for f in new_facts] == ["New Co statement"]


async def test_get_company_profile_resolves_ticker_as_of_not_current_holder(
    monkeypatch, pg_conn
) -> None:
    _patch_pool(monkeypatch, pg_conn)
    company_a, company_b = await _reassigned_ticker(pg_conn, "XYZ")
    await create_profile(
        pg_conn,
        company_id=company_a,
        description="Old Co profile.",
        products_services=["Oil"],
        valid_from=dt.date(2015, 1, 1),
        generated_by="test",
    )
    await create_profile(
        pg_conn,
        company_id=company_b,
        description="New Co profile.",
        products_services=["Software"],
        valid_from=dt.date(2023, 1, 1),
        generated_by="test",
    )

    old_profile = await mcp_mod.get_company_profile("XYZ", "2015-06-01")
    assert old_profile["name"] == "Old Co"
    assert old_profile["description"] == "Old Co profile."

    new_profile = await mcp_mod.get_company_profile("XYZ", "2023-06-01")
    assert new_profile["name"] == "New Co"
    assert new_profile["description"] == "New Co profile."
