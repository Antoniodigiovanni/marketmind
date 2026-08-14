"""Tests for the articles repository — global dedup, per-company attribution."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from data.db.articles import register_article, set_article_status
from data.db.companies import create_company

pytestmark = pytest.mark.asyncio


async def _company(pg_conn, name: str = "TestCo") -> uuid.UUID:
    return await create_company(
        pg_conn, name=name, country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )


async def test_register_article_returns_id_for_a_new_article(pg_conn) -> None:
    company_id = await _company(pg_conn)
    article_id = await register_article(
        pg_conn,
        company_id=company_id,
        url_hash="hash1",
        content_hash="content1",
        url="http://example.com/a",
        source="google-news",
        title="Headline",
        published_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        pre_score=0.5,
    )
    assert isinstance(article_id, uuid.UUID)


async def test_register_article_returns_none_for_same_article_same_company(pg_conn) -> None:
    company_id = await _company(pg_conn)
    kwargs = dict(
        company_id=company_id,
        url_hash="hash1",
        content_hash="content1",
        url="http://example.com/a",
        source="google-news",
        title="Headline",
        published_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        pre_score=0.5,
    )
    first = await register_article(pg_conn, **kwargs)
    second = await register_article(pg_conn, **kwargs)
    assert first is not None
    assert second is None


async def test_register_article_returns_id_for_same_article_different_company(pg_conn) -> None:
    """A1.2: a story reaching two companies' feeds is processed for both,
    not dropped for the second because the article already existed."""
    company_a = await _company(pg_conn, "CompanyA")
    company_b = await _company(pg_conn, "CompanyB")

    first = await register_article(
        pg_conn,
        company_id=company_a,
        url_hash="hash1",
        content_hash="content1",
        url="http://example.com/a",
        source="google-news",
        title="Headline",
        published_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        pre_score=0.5,
    )
    second = await register_article(
        pg_conn,
        company_id=company_b,
        url_hash="hash1",
        content_hash="content1",
        url="http://example.com/a",
        source="google-news",
        title="Headline",
        published_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        pre_score=0.5,
    )
    assert first is not None
    assert second is not None
    assert first == second  # same underlying article_id


async def test_set_article_status_updates_status_error_and_attempts(pg_conn) -> None:
    company_id = await _company(pg_conn)
    article_id = await register_article(
        pg_conn,
        company_id=company_id,
        url_hash="hash1",
        content_hash="content1",
        url="http://example.com/a",
        source="google-news",
        title="Headline",
        published_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        pre_score=0.5,
    )
    await set_article_status(
        pg_conn, article_id=article_id, company_id=company_id, status="error", error="boom"
    )
    row = await (
        await pg_conn.execute(
            "SELECT status, error, attempts FROM article_companies "
            "WHERE article_id = %s AND company_id = %s",
            (article_id, company_id),
        )
    ).fetchone()
    assert row == ("error", "boom", 1)

    await set_article_status(
        pg_conn, article_id=article_id, company_id=company_id, status="processed"
    )
    row = await (
        await pg_conn.execute(
            "SELECT status, error, attempts FROM article_companies "
            "WHERE article_id = %s AND company_id = %s",
            (article_id, company_id),
        )
    ).fetchone()
    assert row == ("processed", None, 2)


async def test_set_article_status_is_scoped_to_the_right_company(pg_conn) -> None:
    company_a = await _company(pg_conn, "CompanyA")
    company_b = await _company(pg_conn, "CompanyB")
    article_id = await register_article(
        pg_conn,
        company_id=company_a,
        url_hash="hash1",
        content_hash="content1",
        url="http://example.com/a",
        source="google-news",
        title="Headline",
        published_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        pre_score=0.5,
    )
    await register_article(
        pg_conn,
        company_id=company_b,
        url_hash="hash1",
        content_hash="content1",
        url="http://example.com/a",
        source="google-news",
        title="Headline",
        published_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        pre_score=0.5,
    )
    await set_article_status(
        pg_conn, article_id=article_id, company_id=company_a, status="processed"
    )
    row_b = await (
        await pg_conn.execute(
            "SELECT status FROM article_companies WHERE article_id = %s AND company_id = %s",
            (article_id, company_b),
        )
    ).fetchone()
    assert row_b[0] == "pending"
