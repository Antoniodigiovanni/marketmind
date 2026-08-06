"""Tests for the versioned fact store."""

from __future__ import annotations

import pytest

from agent.embeddings import ChromaVectorStore
from agent.knowledge import (
    get_company_description,
    get_current_aggregate_facts,
    get_current_facts,
    search_similar_facts,
    upsert_company,
    write_aggregate_fact,
    write_fact,
)
from data.db.init import init_db


def _seed_article(conn, article_id: str = "art-1") -> None:
    conn.execute(
        """
        INSERT INTO articles (id, url_hash, content_hash, url, source, ticker,
                              published_at, fetched_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processed')
        """,
        [
            article_id,
            "h1",
            "c1",
            "https://example.com/1",
            "reuters",
            "AAPL",
            "2026-01-01 09:00:00+00",
            "2026-01-01 10:00:00+00",
        ],
    )


@pytest.fixture()
def conn():
    connection = init_db(":memory:")
    _seed_article(connection)
    yield connection
    connection.close()


def test_write_and_read_fact_round_trip(conn) -> None:
    fact_id = write_fact(
        conn,
        ticker="AAPL",
        statement="AAPL guided Q3 revenue above consensus.",
        fact_type="guidance",
        confidence=0.8,
        valid_from="2026-01-02",
        source_article_id="art-1",
    )

    facts = get_current_facts("AAPL", "2026-01-03", conn)
    assert len(facts) == 1
    assert facts[0]["id"] == fact_id
    assert facts[0]["statement"].startswith("AAPL guided")


def test_point_in_time_boundaries(conn) -> None:
    first = write_fact(
        conn,
        ticker="AAPL",
        statement="CEO is Tim Cook.",
        fact_type="management",
        confidence=0.95,
        valid_from="2026-01-01",
        source_article_id="art-1",
    )
    write_fact(
        conn,
        ticker="AAPL",
        statement="CEO is Jane Doe.",
        fact_type="management",
        confidence=0.95,
        valid_from="2026-02-01",
        source_article_id="art-1",
        supersedes_id=first,
    )

    before = get_current_facts("AAPL", "2026-01-15", conn)
    after = get_current_facts("AAPL", "2026-02-15", conn)
    pre_history = get_current_facts("AAPL", "2025-12-31", conn)

    assert [f["statement"] for f in before] == ["CEO is Tim Cook."]
    assert [f["statement"] for f in after] == ["CEO is Jane Doe."]
    assert pre_history == []


def test_missing_as_of_raises(conn) -> None:
    with pytest.raises(ValueError, match="as_of is required"):
        get_current_facts("AAPL", "", conn)


def test_confidence_out_of_range_raises(conn) -> None:
    with pytest.raises(ValueError, match="confidence"):
        write_fact(
            conn,
            ticker="AAPL",
            statement="Bad confidence.",
            fact_type="risk",
            confidence=1.5,
            valid_from="2026-01-01",
            source_article_id="art-1",
        )


def test_company_description_round_trip(conn) -> None:
    upsert_company(
        conn,
        ticker="AAPL",
        name="Apple Inc.",
        country="US",
        sector="Technology",
        description="Apple designs consumer electronics.",
        description_source="wikipedia",
    )

    assert get_company_description(conn, "AAPL") == "Apple designs consumer electronics."
    assert get_company_description(conn, "NOPE") is None


def test_search_similar_facts_lexical_fallback(conn) -> None:
    write_fact(
        conn,
        ticker="AAPL",
        statement="Apple raised Q3 guidance above consensus.",
        fact_type="guidance",
        confidence=0.8,
        valid_from="2026-01-02",
        source_article_id="art-1",
    )

    similar = search_similar_facts("AAPL", "Apple guidance", "2026-01-03", conn, top_k=5)

    assert len(similar) == 1
    assert similar[0]["statement"].startswith("Apple raised")
    assert similar[0]["score"] > 0


def test_search_similar_facts_empty_without_facts(conn) -> None:
    assert search_similar_facts("AAPL", "anything", "2026-01-03", conn) == []


def test_write_fact_embeds_when_vector_store_given(conn) -> None:
    store = ChromaVectorStore(path=":memory:", collection_name="facts-knowledge-1")

    fact_id = write_fact(
        conn,
        ticker="AAPL",
        statement="Apple raised Q3 guidance above consensus.",
        fact_type="guidance",
        confidence=0.8,
        valid_from="2026-01-02",
        source_article_id="art-1",
        vector_store=store,
    )

    embedding_id = conn.execute(
        "SELECT embedding_id FROM ticker_facts WHERE id = ?", [fact_id]
    ).fetchone()[0]
    assert embedding_id == f"fact:{fact_id}"

    similar = search_similar_facts(
        "AAPL", "Apple guidance", "2026-01-03", conn, vector_store=store
    )
    assert similar and similar[0]["id"] == fact_id


def test_aggregate_fact_write_read_and_supersede(conn) -> None:
    first = write_aggregate_fact(
        conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Tech margins are under pressure.",
        fact_type="margin",
        confidence=0.7,
        valid_from="2026-01-02",
        evidence=["f-1", "f-2"],
    )
    write_aggregate_fact(
        conn,
        dimension="sector",
        dimension_key="Technology",
        statement="Tech margins are stabilising.",
        fact_type="margin",
        confidence=0.8,
        valid_from="2026-02-01",
        evidence=["f-3"],
        supersedes_id=first,
    )

    before = get_current_aggregate_facts("sector", "Technology", "2026-01-15", conn)
    after = get_current_aggregate_facts("sector", "Technology", "2026-02-15", conn)

    assert [f["statement"] for f in before] == ["Tech margins are under pressure."]
    assert before[0]["evidence"] == ["f-1", "f-2"]
    assert [f["statement"] for f in after] == ["Tech margins are stabilising."]


def test_aggregate_fact_requires_valid_dimension(conn) -> None:
    with pytest.raises(ValueError, match="dimension"):
        write_aggregate_fact(
            conn,
            dimension="theme",
            dimension_key="x",
            statement="nope",
            fact_type="risk",
            confidence=0.5,
            valid_from="2026-01-02",
            evidence=[],
        )


def test_aggregate_facts_require_as_of(conn) -> None:
    with pytest.raises(ValueError, match="as_of"):
        get_current_aggregate_facts("sector", "Technology", "", conn)
