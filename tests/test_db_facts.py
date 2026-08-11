"""Tests for the facts repository — point-in-time reads, writes, vector search."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from data.db.companies import create_company
from data.db.facts import create_fact, get_current_facts, search_similar_facts

pytestmark = pytest.mark.asyncio

NEAR_VECTOR = [1.0] + [0.0] * 1535
FAR_VECTOR = [0.0, 1.0] + [0.0] * 1534
ZERO_VECTOR = [0.0] * 1536


def _stub_embed(mapping: dict[str, list[float]]):
    """Build an async embed stub returning a fixed vector per known text."""

    async def embed(text: str) -> list[float]:
        return mapping[text]

    return embed


async def _company(pg_conn, name: str = "TestCo") -> uuid.UUID:
    return await create_company(
        pg_conn, name=name, country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )


async def _article(pg_conn) -> uuid.UUID:
    row = await (
        await pg_conn.execute(
            "INSERT INTO articles (url_hash, content_hash, url, source, published_at) "
            "VALUES (%s, %s, %s, %s, NOW()) RETURNING article_id",
            (uuid.uuid4().hex, uuid.uuid4().hex, "http://example.com", "test"),
        )
    ).fetchone()
    return row[0]


async def test_create_fact_returns_a_uuid(pg_conn) -> None:
    company_id = await _company(pg_conn)
    article_id = await _article(pg_conn)
    fact_id = await create_fact(
        pg_conn,
        company_id=company_id,
        statement="Revenue grew 12% YoY",
        fact_type="guidance",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=_stub_embed({"Revenue grew 12% YoY": ZERO_VECTOR}),
        model="test",
    )
    assert isinstance(fact_id, uuid.UUID)


async def test_get_current_facts_returns_what_was_created(pg_conn) -> None:
    company_id = await _company(pg_conn)
    article_id = await _article(pg_conn)
    await create_fact(
        pg_conn,
        company_id=company_id,
        statement="Revenue grew 12% YoY",
        fact_type="guidance",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=_stub_embed({"Revenue grew 12% YoY": ZERO_VECTOR}),
        model="test",
    )
    facts = await get_current_facts(pg_conn, company_id=company_id, as_of=dt.date(2020, 6, 1))
    assert len(facts) == 1
    assert facts[0]["statement"] == "Revenue grew 12% YoY"
    assert facts[0]["fact_type"] == "guidance"
    assert facts[0]["confidence"] == 0.9


async def test_get_current_facts_is_point_in_time(pg_conn) -> None:
    company_id = await _company(pg_conn)
    article_id = await _article(pg_conn)
    await create_fact(
        pg_conn,
        company_id=company_id,
        statement="Revenue grew 12% YoY",
        fact_type="guidance",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=_stub_embed({"Revenue grew 12% YoY": ZERO_VECTOR}),
        model="test",
    )
    before = await get_current_facts(pg_conn, company_id=company_id, as_of=dt.date(2019, 12, 31))
    assert before == []


async def test_create_fact_rejects_bad_confidence_without_calling_embed(pg_conn) -> None:
    calls: list[str] = []

    async def embed(text: str) -> list[float]:
        calls.append(text)
        return ZERO_VECTOR

    company_id = await _company(pg_conn)
    article_id = await _article(pg_conn)
    with pytest.raises(ValueError, match=r"confidence must be in \[0, 1\]"):
        await create_fact(
            pg_conn,
            company_id=company_id,
            statement="Bad fact",
            fact_type="guidance",
            confidence=1.5,
            valid_from=dt.date(2020, 1, 1),
            source_article_id=article_id,
            embed=embed,
            model="test",
        )
    assert calls == []


async def test_create_fact_with_supersedes_closes_the_old_fact(pg_conn) -> None:
    company_id = await _company(pg_conn)
    article_id = await _article(pg_conn)
    embed = _stub_embed({"Old guidance": ZERO_VECTOR, "New guidance": ZERO_VECTOR})

    old_id = await create_fact(
        pg_conn,
        company_id=company_id,
        statement="Old guidance",
        fact_type="guidance",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=embed,
        model="test",
    )
    new_id = await create_fact(
        pg_conn,
        company_id=company_id,
        statement="New guidance",
        fact_type="guidance",
        confidence=0.9,
        valid_from=dt.date(2021, 1, 1),
        source_article_id=article_id,
        embed=embed,
        model="test",
        supersedes_id=old_id,
    )

    as_of_before = await get_current_facts(
        pg_conn, company_id=company_id, as_of=dt.date(2020, 6, 1)
    )
    assert [f["fact_id"] for f in as_of_before] == [old_id]

    as_of_after = await get_current_facts(pg_conn, company_id=company_id, as_of=dt.date(2021, 6, 1))
    assert [f["fact_id"] for f in as_of_after] == [new_id]

    row = await (
        await pg_conn.execute(
            "SELECT supersedes_id FROM company_facts WHERE fact_id = %s", (new_id,)
        )
    ).fetchone()
    assert row[0] == old_id


async def test_search_similar_facts_orders_by_similarity(pg_conn) -> None:
    company_id = await _company(pg_conn)
    article_id = await _article(pg_conn)
    embed = _stub_embed(
        {
            "Revenue grew 12% YoY": NEAR_VECTOR,
            "New CEO appointed": FAR_VECTOR,
            "query": NEAR_VECTOR,
        }
    )
    await create_fact(
        pg_conn,
        company_id=company_id,
        statement="Revenue grew 12% YoY",
        fact_type="guidance",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=embed,
        model="test",
    )
    await create_fact(
        pg_conn,
        company_id=company_id,
        statement="New CEO appointed",
        fact_type="management",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=embed,
        model="test",
    )

    results = await search_similar_facts(
        pg_conn, company_id=company_id, query="query", as_of=dt.date(2020, 6, 1), embed=embed
    )
    assert [r["statement"] for r in results] == ["Revenue grew 12% YoY", "New CEO appointed"]
    assert results[0]["similarity"] == pytest.approx(1.0, abs=1e-6)
    assert results[0]["similarity"] > results[1]["similarity"]


async def test_search_similar_facts_excludes_facts_retired_before_as_of(pg_conn) -> None:
    """A0.3 regression: the closest vector match must not surface if it was
    retired before as_of, even though nothing about vector distance knows
    that."""
    company_id = await _company(pg_conn)
    article_id = await _article(pg_conn)
    embed = _stub_embed(
        {
            "Old guidance": NEAR_VECTOR,  # closest to the query...
            "New guidance": FAR_VECTOR,  # ...but this is what should survive
            "query": NEAR_VECTOR,
        }
    )
    old_id = await create_fact(
        pg_conn,
        company_id=company_id,
        statement="Old guidance",
        fact_type="guidance",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=embed,
        model="test",
    )
    await create_fact(
        pg_conn,
        company_id=company_id,
        statement="New guidance",
        fact_type="guidance",
        confidence=0.9,
        valid_from=dt.date(2021, 1, 1),
        source_article_id=article_id,
        embed=embed,
        model="test",
        supersedes_id=old_id,
    )

    results = await search_similar_facts(
        pg_conn, company_id=company_id, query="query", as_of=dt.date(2021, 6, 1), embed=embed
    )
    assert [r["statement"] for r in results] == ["New guidance"]


async def test_search_similar_facts_is_scoped_to_company(pg_conn) -> None:
    company_a = await _company(pg_conn, "CompanyA")
    company_b = await _company(pg_conn, "CompanyB")
    article_id = await _article(pg_conn)
    embed = _stub_embed({"Shared-sounding statement": NEAR_VECTOR, "query": NEAR_VECTOR})

    await create_fact(
        pg_conn,
        company_id=company_b,
        statement="Shared-sounding statement",
        fact_type="guidance",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=embed,
        model="test",
    )

    results = await search_similar_facts(
        pg_conn, company_id=company_a, query="query", as_of=dt.date(2020, 6, 1), embed=embed
    )
    assert results == []


async def test_create_fact_is_atomic_when_embed_raises(pg_conn) -> None:
    company_id = await _company(pg_conn)
    article_id = await _article(pg_conn)

    async def failing_embed(text: str) -> list[float]:
        raise RuntimeError("embedding service unavailable")

    with pytest.raises(RuntimeError, match="embedding service unavailable"):
        await create_fact(
            pg_conn,
            company_id=company_id,
            statement="Should not persist",
            fact_type="guidance",
            confidence=0.9,
            valid_from=dt.date(2020, 1, 1),
            source_article_id=article_id,
            embed=failing_embed,
            model="test",
        )
    row = await (
        await pg_conn.execute("SELECT 1 FROM company_facts WHERE statement = 'Should not persist'")
    ).fetchone()
    assert row is None
