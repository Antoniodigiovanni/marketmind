"""Tests for the versioned fact store."""

from __future__ import annotations

import pytest

from agent.knowledge import get_current_facts, write_fact
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
