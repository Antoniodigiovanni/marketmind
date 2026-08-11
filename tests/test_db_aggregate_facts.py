"""Tests for the aggregate facts repository — point-in-time, evidence, vectors."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest

from data.db.aggregate_facts import (
    create_aggregate_fact,
    get_current_aggregate_facts,
    search_similar_aggregate_facts,
)
from data.db.companies import create_company
from data.db.facts import create_fact

pytestmark = pytest.mark.asyncio

NEAR_VECTOR = [1.0] + [0.0] * 1535
FAR_VECTOR = [0.0, 1.0] + [0.0] * 1534
ZERO_VECTOR = [0.0] * 1536


def _stub_embed(mapping: dict[str, list[float]]):
    """Build an async embed stub returning a fixed vector per known text."""

    async def embed(text: str) -> list[float]:
        return mapping[text]

    return embed


async def _article(pg_conn) -> uuid.UUID:
    row = await (
        await pg_conn.execute(
            "INSERT INTO articles (url_hash, content_hash, url, source, published_at) "
            "VALUES (%s, %s, %s, %s, NOW()) RETURNING article_id",
            (uuid.uuid4().hex, uuid.uuid4().hex, "http://example.com", "test"),
        )
    ).fetchone()
    return row[0]


async def _company_fact(pg_conn, statement: str = "Some fact") -> uuid.UUID:
    """Create a real company_facts row for use as evidence."""
    company_id = await create_company(
        pg_conn, name="TestCo", country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )
    article_id = await _article(pg_conn)
    return await create_fact(
        pg_conn,
        company_id=company_id,
        statement=statement,
        fact_type="guidance",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        source_article_id=article_id,
        embed=_stub_embed({statement: ZERO_VECTOR}),
        model="test",
    )


async def test_create_aggregate_fact_returns_a_uuid(pg_conn) -> None:
    aggregate_id = await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Tech sector revenue growth accelerating",
        fact_type="trend",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=_stub_embed({"Tech sector revenue growth accelerating": ZERO_VECTOR}),
        model="test",
    )
    assert isinstance(aggregate_id, uuid.UUID)


async def test_create_aggregate_fact_rejects_bad_dimension_without_calling_embed(pg_conn) -> None:
    calls: list[str] = []

    async def embed(text: str) -> list[float]:
        calls.append(text)
        return ZERO_VECTOR

    with pytest.raises(ValueError, match="dimension must be one of"):
        await create_aggregate_fact(
            pg_conn,
            dimension="not-a-dimension",
            dimension_key="Technology",
            statement="Bad",
            fact_type="trend",
            confidence=0.8,
            valid_from=dt.date(2020, 1, 1),
            evidence=[],
            embed=embed,
            model="test",
        )
    assert calls == []


async def test_create_aggregate_fact_rejects_bad_confidence_without_calling_embed(pg_conn) -> None:
    calls: list[str] = []

    async def embed(text: str) -> list[float]:
        calls.append(text)
        return ZERO_VECTOR

    with pytest.raises(ValueError, match=r"confidence must be in \[0, 1\]"):
        await create_aggregate_fact(
            pg_conn,
            dimension="sector",
            dimension_key="Technology",
            statement="Bad",
            fact_type="trend",
            confidence=1.5,
            valid_from=dt.date(2020, 1, 1),
            evidence=[],
            embed=embed,
            model="test",
        )
    assert calls == []


async def test_get_current_aggregate_facts_returns_evidence(pg_conn) -> None:
    fact_a = await _company_fact(pg_conn, "Company A guidance")
    fact_b = await _company_fact(pg_conn, "Company B guidance")
    embed = _stub_embed({"Sector trend": ZERO_VECTOR})

    await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Sector trend",
        fact_type="trend",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        evidence=[fact_a, fact_b],
        embed=embed,
        model="test",
    )

    facts = await get_current_aggregate_facts(
        pg_conn, dimension="sector", dimension_key="Technology", as_of=dt.date(2020, 6, 1)
    )
    assert len(facts) == 1
    assert set(facts[0]["evidence"]) == {fact_a, fact_b}


async def test_get_current_aggregate_facts_evidence_is_empty_list_when_none(pg_conn) -> None:
    embed = _stub_embed({"No evidence trend": ZERO_VECTOR})
    await create_aggregate_fact(
        pg_conn,
        dimension="geography",
        dimension_key="US",
        statement="No evidence trend",
        fact_type="trend",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )
    facts = await get_current_aggregate_facts(
        pg_conn, dimension="geography", dimension_key="US", as_of=dt.date(2020, 6, 1)
    )
    assert facts[0]["evidence"] == []


async def test_get_current_aggregate_facts_is_point_in_time(pg_conn) -> None:
    embed = _stub_embed({"Trend": ZERO_VECTOR})
    await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Trend",
        fact_type="trend",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )
    before = await get_current_aggregate_facts(
        pg_conn, dimension="sector", dimension_key="Technology", as_of=dt.date(2019, 12, 31)
    )
    assert before == []


async def test_get_current_aggregate_facts_is_scoped_to_dimension_and_key(pg_conn) -> None:
    """Neither a different dimension_key nor a different dimension sharing
    the same dimension_key string may leak into a get_current_aggregate_facts
    call -- both columns of the composite filter must hold.
    """
    embed = _stub_embed({"Healthcare trend": ZERO_VECTOR, "Wrong dimension same key": ZERO_VECTOR})
    await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Healthcare",
        statement="Healthcare trend",
        fact_type="trend",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )
    await create_aggregate_fact(
        pg_conn,
        dimension="geography",
        dimension_key="Technology",
        statement="Wrong dimension same key",
        fact_type="trend",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )
    facts = await get_current_aggregate_facts(
        pg_conn, dimension="sector", dimension_key="Technology", as_of=dt.date(2020, 6, 1)
    )
    assert facts == []


async def test_create_aggregate_fact_with_supersedes_closes_the_old_fact(pg_conn) -> None:
    embed = _stub_embed({"Old trend": ZERO_VECTOR, "New trend": ZERO_VECTOR})
    old_id = await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Old trend",
        fact_type="trend",
        confidence=0.7,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )
    new_id = await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="New trend",
        fact_type="trend",
        confidence=0.9,
        valid_from=dt.date(2021, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
        supersedes_id=old_id,
    )

    before = await get_current_aggregate_facts(
        pg_conn, dimension="sector", dimension_key="Technology", as_of=dt.date(2020, 6, 1)
    )
    assert [f["aggregate_id"] for f in before] == [old_id]

    after = await get_current_aggregate_facts(
        pg_conn, dimension="sector", dimension_key="Technology", as_of=dt.date(2021, 6, 1)
    )
    assert [f["aggregate_id"] for f in after] == [new_id]


async def test_search_similar_aggregate_facts_orders_by_similarity(pg_conn) -> None:
    embed = _stub_embed(
        {
            "Strong tech growth": NEAR_VECTOR,
            "Weak retail growth": FAR_VECTOR,
            "query": NEAR_VECTOR,
        }
    )
    await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Strong tech growth",
        fact_type="trend",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )
    await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Weak retail growth",
        fact_type="trend",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )

    results = await search_similar_aggregate_facts(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        query="query",
        as_of=dt.date(2020, 6, 1),
        embed=embed,
    )
    assert [r["statement"] for r in results] == ["Strong tech growth", "Weak retail growth"]
    assert results[0]["similarity"] == pytest.approx(1.0, abs=1e-6)
    assert results[0]["similarity"] > results[1]["similarity"]


async def test_search_similar_aggregate_facts_excludes_facts_retired_before_as_of(pg_conn) -> None:
    embed = _stub_embed(
        {
            "Old trend": NEAR_VECTOR,
            "New trend": FAR_VECTOR,
            "query": NEAR_VECTOR,
        }
    )
    old_id = await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Old trend",
        fact_type="trend",
        confidence=0.8,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )
    await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        statement="New trend",
        fact_type="trend",
        confidence=0.9,
        valid_from=dt.date(2021, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
        supersedes_id=old_id,
    )

    results = await search_similar_aggregate_facts(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        query="query",
        as_of=dt.date(2021, 6, 1),
        embed=embed,
    )
    assert [r["statement"] for r in results] == ["New trend"]


async def test_search_similar_aggregate_facts_is_scoped_to_dimension_and_key(pg_conn) -> None:
    """Neither a different dimension_key nor a different dimension sharing
    the same dimension_key string may surface in search results -- both
    columns of the composite filter must hold.
    """
    embed = _stub_embed({"Shared-sounding statement": NEAR_VECTOR, "query": NEAR_VECTOR})

    await create_aggregate_fact(
        pg_conn,
        dimension="sector",
        dimension_key="Healthcare",
        statement="Shared-sounding statement",
        fact_type="trend",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )
    await create_aggregate_fact(
        pg_conn,
        dimension="geography",
        dimension_key="Technology",
        statement="Shared-sounding statement",
        fact_type="trend",
        confidence=0.9,
        valid_from=dt.date(2020, 1, 1),
        evidence=[],
        embed=embed,
        model="test",
    )

    results = await search_similar_aggregate_facts(
        pg_conn,
        dimension="sector",
        dimension_key="Technology",
        query="query",
        as_of=dt.date(2020, 6, 1),
        embed=embed,
    )
    assert results == []


async def test_create_aggregate_fact_is_atomic_when_embed_raises(pg_conn) -> None:
    async def failing_embed(text: str) -> list[float]:
        raise RuntimeError("embedding service unavailable")

    with pytest.raises(RuntimeError, match="embedding service unavailable"):
        await create_aggregate_fact(
            pg_conn,
            dimension="sector",
            dimension_key="Technology",
            statement="Should not persist",
            fact_type="trend",
            confidence=0.9,
            valid_from=dt.date(2020, 1, 1),
            evidence=[],
            embed=failing_embed,
            model="test",
        )
    row = await (
        await pg_conn.execute(
            "SELECT 1 FROM aggregate_facts WHERE statement = 'Should not persist'"
        )
    ).fetchone()
    assert row is None


async def test_create_aggregate_fact_rolls_back_evidence_when_embedding_insert_fails(
    pg_conn,
) -> None:
    """Mid-transaction rollback: embed succeeds with the wrong dimension, so
    the aggregate_fact_embeddings insert fails after both the aggregate_facts
    and aggregate_fact_evidence inserts have already run -- verifying the
    savepoint rolls back all three, not just the first."""
    fact_a = await _company_fact(pg_conn, "Evidence fact")

    async def wrong_dimension_embed(text: str) -> list[float]:
        return [0.0, 0.0, 0.0]

    with pytest.raises(psycopg.errors.DataException):
        await create_aggregate_fact(
            pg_conn,
            dimension="sector",
            dimension_key="Technology",
            statement="Should also not persist",
            fact_type="trend",
            confidence=0.9,
            valid_from=dt.date(2020, 1, 1),
            evidence=[fact_a],
            embed=wrong_dimension_embed,
            model="test",
        )
    row = await (
        await pg_conn.execute(
            "SELECT 1 FROM aggregate_facts WHERE statement = 'Should also not persist'"
        )
    ).fetchone()
    assert row is None
