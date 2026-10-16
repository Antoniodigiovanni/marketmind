"""Date ownership, cutoff, terminal replay and advisory lock regression checks."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from unittest.mock import AsyncMock

import polars as pl
import pytest

from agent import ingestion
from data.db.articles import get_article_work, register_article
from data.db.companies import create_company
from data.sources.rss import Article


@pytest.mark.asyncio
async def test_date_scoped_snapshots_and_legacy_exclusion(pg_conn) -> None:
    """Unknown legacy dates are excluded; snapshots remain owned by their date."""
    company = await create_company(
        pg_conn, name="Date test", country=None, sector=None, first_seen=dt.date(2020, 1, 1)
    )
    kwargs = dict(
        company_id=company,
        url_hash="date-url",
        content_hash="date-content",
        url="https://example.invalid/date",
        source="google-news",
        title="Date",
        published_at=dt.datetime(2026, 8, 4, tzinfo=dt.UTC),
        pre_score=0.5,
    )
    await register_article(pg_conn, **kwargs)
    day = dt.date(2026, 8, 4)
    assert await get_article_work(pg_conn, run_date=day) == []
    first = await register_article(pg_conn, **kwargs, run_date=day, snapshot={"ticker": "OLD"})
    assert first is not None
    assert (
        await register_article(pg_conn, **kwargs, run_date=day, snapshot={"ticker": "NEW"}) is None
    )
    later = day + dt.timedelta(days=1)
    await register_article(pg_conn, **kwargs, run_date=later, snapshot={"ticker": "LATER"})
    assert (await get_article_work(pg_conn, run_date=day))[0]["snapshot"] == {"ticker": "OLD"}
    assert await get_article_work(pg_conn, run_date=later) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "publication", ["", "invalid", "2026-08-04T09:00:00", "2026-08-05T00:00:00+00:00"]
)
async def test_cutoff_before_registration(pg_conn, tmp_path, publication) -> None:
    """Invalid and next-midnight publications never become replay candidates."""
    universe = pl.DataFrame(
        {"ticker": ["CUT"], "name": ["Cutoff"], "sector": ["Test"], "country": ["US"]}
    )
    article = Article(
        "CUT", "https://example.invalid/cut", "Cutoff", "Summary", "google-news", publication
    )
    result = await ingestion.run_daily_ingest(
        pg_conn,
        "2026-08-04",
        universe=universe,
        news_fetcher=lambda *_: [article],
        description_fetcher=lambda _: "Company",
        profile_builder=lambda _: None,
        llm=lambda _: pytest.fail("ineligible article reached extraction"),
        raw_news_dir=tmp_path,
    )
    assert result["articles_filtered"] == 1
    assert await get_article_work(pg_conn, run_date=dt.date(2026, 8, 4)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("aggregate_response", ["REDUNDANT", "[]"])
async def test_empty_aggregate_completed_once(pg_conn, tmp_path, aggregate_response) -> None:
    """Empty aggregate results survive replay even with a changed universe."""
    universe = pl.DataFrame(
        {"ticker": ["ONCE"], "name": ["Once"], "sector": ["Once sector"], "country": ["US"]}
    )
    article = Article(
        "ONCE",
        "https://example.invalid/once",
        "Once",
        "Summary",
        "google-news",
        "2026-08-04T09:00:00+00:00",
    )
    aggregate_calls = 0

    def llm(messages):
        nonlocal aggregate_calls
        if "aggregate" in messages[-1]["content"]:
            aggregate_calls += 1
            return aggregate_response
        return json.dumps([{"statement": "One development", "confidence": 0.8}])

    options = dict(
        description_fetcher=lambda _: "Company",
        profile_builder=lambda _: None,
        llm=llm,
        raw_news_dir=tmp_path,
    )
    await ingestion.run_daily_ingest(
        pg_conn, "2026-08-04", universe=universe, news_fetcher=lambda *_: [article], **options
    )
    assert aggregate_calls == 2
    await ingestion.run_daily_ingest(
        pg_conn, "2026-08-04", universe=universe.head(0), news_fetcher=lambda *_: [], **options
    )
    assert aggregate_calls == 2


@pytest.mark.asyncio
async def test_advisory_lock_released_on_exception_and_cancellation() -> None:
    """A pooled session cannot retain the ingestion lock after interrupted work."""
    for error in (RuntimeError("failed"), asyncio.CancelledError()):
        conn = AsyncMock()

        @ingestion._serialized_ingest
        async def interrupted(conn, failure=error):
            raise failure

        with pytest.raises(type(error)):
            await interrupted(conn)
        assert conn.execute.await_args_list[0].args == ("SELECT pg_advisory_lock(731942617028)",)
        assert conn.execute.await_args_list[-1].args == ("SELECT pg_advisory_unlock(731942617028)",)


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", ["invalid", "2026-08-04T09:00:00", "2026-08-05T00:00:00Z"])
async def test_invalid_persisted_publication_never_reaches_model(pg_conn, tmp_path, publication):
    """Replay applies the same cutoff even to imported legacy payloads."""
    company = await create_company(
        pg_conn, name="Legacy", country=None, sector=None, first_seen=dt.date(2020, 1, 1)
    )
    await register_article(
        pg_conn,
        company_id=company,
        url_hash="legacy-url",
        content_hash="legacy-content",
        url="https://example.invalid/legacy",
        source="google-news",
        title="Legacy",
        published_at=dt.datetime(2026, 8, 4, tzinfo=dt.UTC),
        pre_score=0.5,
        run_date=dt.date(2026, 8, 4),
        snapshot=_snapshot(publication),
    )
    await ingestion.run_daily_ingest(
        pg_conn,
        "2026-08-04",
        universe=pl.DataFrame({"ticker": [], "name": []}),
        llm=lambda _: pytest.fail("invalid replay reached extraction"),
        raw_news_dir=tmp_path,
    )
    assert await (await pg_conn.execute("SELECT 1 FROM company_facts")).fetchone() is None
    if publication != "2026-08-05T00:00:00Z":
        row = await (
            await pg_conn.execute(
                "SELECT status, error FROM article_companies WHERE company_id=%s", (company,)
            )
        ).fetchone()
        assert row[0] == "error"
        assert "published_at" in row[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("malformation", "context_field", "invalid_value"),
    [
        (case, None, None)
        for case in ("empty", "null_article", "missing_title", "string_score", "huge_integer_score")
    ]
    + [
        pytest.param(
            "invalid_context",
            field,
            [[]] if field == "products_services" and kind == "array" else value,
            id=f"{field}-{kind}",
        )
        for field in ("ticker", "name", "sector", "country", "description", "products_services")
        for kind, value in (
            ("null", None),
            ("boolean", True),
            ("integer", 42),
            ("float", 1.5),
            ("object", {"unexpected": "value"}),
            ("array", []),
        )
        if kind != "null" or field not in ("sector", "country")
    ]
    + [
        pytest.param("invalid_context", "products_services", [value], id=f"products-item-{kind}")
        for kind, value in (
            ("null", None),
            ("boolean", True),
            ("integer", 42),
            ("float", 1.5),
            ("object", {"unexpected": "value"}),
        )
    ],
)
@pytest.mark.parametrize(
    "products_services", ["Original products and services", [], ["One", "Two"]]
)
async def test_malformed_snapshot_does_not_obstruct_valid_following_work(
    monkeypatch, tmp_path, malformation, context_field, invalid_value, products_services
):
    """One corrupt durable payload must not permanently block later eligible work."""
    from unittest.mock import MagicMock

    record = dict(
        ticker="GOOD",
        url="https://example.invalid/good",
        title="Good",
        summary="Summary",
        source="google-news",
        published_at="2026-08-04T09:00:00Z",
    )
    snapshot = dict(
        ticker="GOOD",
        name="Original company",
        sector=None if products_services == [] else "Original sector",
        country=None,
        description="Original description",
        products_services=products_services,
        article=record,
        score=1.0,
    )
    bad = {**snapshot, "article": dict(record)}
    if malformation == "empty":
        bad = {}
    elif malformation == "null_article":
        bad["article"] = None
    elif malformation == "missing_title":
        del bad["article"]["title"]
    elif malformation == "huge_integer_score":
        bad["score"] = 10**400
    elif malformation == "invalid_context":
        bad[context_field] = invalid_value
    else:
        bad["score"] = "invalid"
    for name in ("start_run", "finish_run", "set_article_status", "finish_article_work"):
        monkeypatch.setattr(ingestion, name, AsyncMock())
    monkeypatch.setattr(
        ingestion,
        "get_article_work",
        AsyncMock(
            return_value=[
                dict(article_id="bad", company_id="company", snapshot=bad),
                dict(article_id="good", company_id="company", snapshot=snapshot),
            ]
        ),
    )
    monkeypatch.setattr(ingestion, "get_current_facts", AsyncMock(return_value=[]))
    monkeypatch.setattr(ingestion, "search_similar_facts", AsyncMock(return_value=[]))
    monkeypatch.setattr(ingestion, "lock_article_work", AsyncMock(return_value=True))
    monkeypatch.setattr(ingestion, "aggregate_inputs", AsyncMock(return_value=[]))
    conn = MagicMock()
    conn.execute = AsyncMock()
    calls = []
    summary = await ingestion.run_daily_ingest(
        conn,
        "2026-08-04",
        universe=pl.DataFrame({"ticker": [], "name": []}),
        llm=lambda messages: calls.append(messages) or "REDUNDANT",
        raw_news_dir=tmp_path,
    )
    for status_writer in (ingestion.set_article_status, ingestion.finish_article_work):
        assert any(
            call.kwargs.get("article_id") == "bad" and call.kwargs.get("status") == "error"
            for call in status_writer.await_args_list
        )
    assert "snapshot" in ingestion.set_article_status.await_args_list[0].kwargs["error"]
    assert ingestion.get_current_facts.await_count == 1
    assert ingestion.search_similar_facts.await_count == 1
    assert len(calls) == 1
    assert "Original company" in str(calls[0])
    if snapshot["sector"] is not None:
        assert "Original sector" in str(calls[0])
    assert "Original description" in str(calls[0])
    expected_products = (
        ", ".join(products_services) if isinstance(products_services, list) else products_services
    )
    assert (expected_products or "(none)") in str(calls[0])
    assert summary["articles_errored"] == 1
    assert any(
        call.kwargs.get("article_id") == "good" and call.kwargs.get("status") == "redundant"
        for call in ingestion.finish_article_work.await_args_list
    )


@pytest.mark.asyncio
async def test_real_0004_upgrade_preserves_legacy_pairs(pg_conn):
    """Execute 0005 against actual pre-replay tables and legacy pair statuses."""
    import data.db.init_postgres as migrations

    # All DDL is transaction-local in a unique schema, preserving pooled public OIDs.
    schema = f"article_upgrade_{uuid.uuid4().hex}"
    await pg_conn.execute(f'CREATE SCHEMA "{schema}"')
    await pg_conn.execute(f'SET LOCAL search_path TO "{schema}", public')
    await pg_conn.execute(migrations.POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8"))
    await pg_conn.execute(
        f'CREATE TABLE "{schema}".schema_migrations AS '
        "SELECT * FROM marketmind_meta.schema_migrations WHERE version < 5"
    )
    await pg_conn.execute(
        f'DROP TABLE "{schema}".article_aggregate_inputs, "{schema}".article_fact_inputs, '
        f'"{schema}".article_aggregate_work, "{schema}".article_work'
    )
    company = await create_company(
        pg_conn, name="Upgrade legacy", country=None, sector=None, first_seen=dt.date(2020, 1, 1)
    )
    pairs = []
    for status in ("pending", "error", "processed"):
        article = await register_article(
            pg_conn,
            company_id=company,
            url_hash=f"upgrade-{status}",
            content_hash=f"upgrade-{status}",
            url=f"https://example.invalid/{status}",
            source="test",
            title=status,
            published_at=dt.datetime(2026, 8, 4, tzinfo=dt.UTC),
            pre_score=1.0,
        )
        await pg_conn.execute(
            "UPDATE article_companies SET status=%s WHERE article_id=%s", (status, article)
        )
        pairs.append((article, status))

    class TransactionConnection:
        async def execute(self, sql, params=None):
            sql = sql.replace("marketmind_meta.schema_migrations", f'"{schema}".schema_migrations')
            sql = sql.replace("table_schema = 'public'", f"table_schema = '{schema}'")
            return await pg_conn.execute(sql, params)

        async def commit(self):
            # Keep the real migration SQL and ledger writes inside the fixture rollback.
            pass

    # Run the production migration dispatcher, including checksum and ledger handling.
    await migrations._apply_pending_migrations(TransactionConnection())
    assert await get_article_work(pg_conn, run_date=dt.date(2026, 8, 4)) == []
    for article, status in pairs:
        row = await (
            await pg_conn.execute(
                "SELECT status FROM article_companies WHERE article_id=%s", (article,)
            )
        ).fetchone()
        assert row == (status,)
    row = await (
        await pg_conn.execute(
            f'SELECT name, checksum FROM "{schema}".schema_migrations WHERE version=5'
        )
    ).fetchone()
    migration = migrations._migration_files()[-1]
    assert row == ("article_replay", migration[2])
    await migrations._apply_pending_migrations(TransactionConnection())
    assert await (await pg_conn.execute("SELECT count(*) FROM article_work")).fetchone() == (0,)


def _snapshot(publication: str = "2026-08-04T09:00:00Z") -> dict[str, object]:
    """Return a complete replay snapshot to exercise publication validation."""
    return dict(
        ticker="LEGACY",
        name="Legacy",
        sector="Test",
        country="US",
        description="Test company",
        products_services=[],
        score=1.0,
        article=dict(
            ticker="LEGACY",
            url="https://example.invalid/legacy",
            title="Legacy report",
            summary="A material development",
            source="google-news",
            published_at=publication,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", ["pending", "error"])
@pytest.mark.parametrize("stage", ["registration", "replay"])
async def test_persisted_future_publication_blocks_historical_work(
    pg_conn, tmp_path, legacy_status, stage
) -> None:
    """Incoming eligible timestamps cannot override the stored publication cutoff."""
    company = await create_company(
        pg_conn, name="Legacy", country=None, sector=None, first_seen=dt.date(2020, 1, 1)
    )
    day = dt.date(2026, 8, 4)
    kwargs = dict(
        company_id=company,
        url_hash="future-url",
        content_hash="future-content",
        url="https://example.invalid/legacy",
        source="google-news",
        title="Legacy",
        pre_score=1.0,
    )
    article_id = await register_article(
        pg_conn, **kwargs, published_at=dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
    )
    await pg_conn.execute(
        "UPDATE article_companies SET status=%s WHERE article_id=%s", (legacy_status, article_id)
    )
    if stage == "registration":
        registered = await register_article(
            pg_conn,
            **kwargs,
            published_at=dt.datetime(2026, 8, 4, 9, tzinfo=dt.UTC),
            run_date=day,
            snapshot=_snapshot(),
        )
    else:
        # Bypass registration to test work imported before a registration guard exists.
        await pg_conn.execute(
            "INSERT INTO article_work (article_id, company_id, run_date, snapshot) "
            "VALUES (%s, %s, %s, %s::jsonb)",
            (article_id, company, day, json.dumps(_snapshot())),
        )
    calls = []
    await ingestion.run_daily_ingest(
        pg_conn,
        day.isoformat(),
        universe=pl.DataFrame({"ticker": [], "name": []}),
        llm=lambda messages: (
            calls.append(messages)
            or json.dumps([{"statement": "Historical fact", "confidence": 0.8}])
        ),
        raw_news_dir=tmp_path,
    )
    assert (
        await (
            await pg_conn.execute(
                "SELECT 1 FROM company_facts WHERE source_article=%s", (article_id,)
            )
        ).fetchone()
        is None
    )
    assert calls == []
    assert await (
        await pg_conn.execute(
            "SELECT published_at FROM articles WHERE article_id=%s", (article_id,)
        )
    ).fetchone() == (dt.datetime(2026, 8, 5, tzinfo=dt.UTC),)
    if stage == "registration":
        assert registered is None
        assert await get_article_work(pg_conn, run_date=day) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["create_fact", "embedding"])
async def test_successful_extractions_with_repeated_write_failures_abort(
    pg_conn, monkeypatch, tmp_path, failure_site
) -> None:
    """Successful parsing cannot reset persistence failures or suppress aborted audits."""
    threshold = 3
    monkeypatch.setattr(ingestion, "MAX_CONSECUTIVE_FAILURES", threshold)
    monkeypatch.setattr(ingestion, "search_similar_facts", AsyncMock(return_value=[]))
    company = await create_company(
        pg_conn, name="Legacy", country=None, sector=None, first_seen=dt.date(2020, 1, 1)
    )
    day = dt.date(2026, 8, 4)
    article_ids = []
    for index in range(threshold + 1):
        article_ids.append(
            await register_article(
                pg_conn,
                company_id=company,
                url_hash=f"failure-{index}",
                content_hash=f"failure-{index}",
                url=f"https://example.invalid/failure/{index}",
                source="google-news",
                title=f"Report {index}",
                published_at=dt.datetime(2026, 8, 4, 9, tzinfo=dt.UTC),
                pre_score=1.0,
                run_date=day,
                snapshot=_snapshot(),
            )
        )
    failure = AsyncMock(side_effect=RuntimeError(f"injected {failure_site} failure"))
    monkeypatch.setattr(
        ingestion, "create_fact" if failure_site == "create_fact" else "hash_embed", failure
    )
    calls = []

    def llm(messages):
        calls.append(messages)
        return json.dumps([{"statement": "Valid extracted fact", "confidence": 0.8}])

    with pytest.raises(ingestion.IngestAborted):
        await ingestion.run_daily_ingest(
            pg_conn,
            day.isoformat(),
            universe=pl.DataFrame({"ticker": [], "name": []}),
            llm=llm,
            raw_news_dir=tmp_path,
        )
    assert len(calls) == threshold
    assert failure.await_count == threshold
    audit = await (
        await pg_conn.execute(
            "SELECT status, counters, error FROM agent_runs WHERE job_type='news' "
            "ORDER BY started_at DESC LIMIT 1"
        )
    ).fetchone()
    assert audit[0] == "aborted"
    assert audit[1]["articles_errored"] == threshold
    assert audit[1]["facts_written"] == 0
    assert "consecutive" in audit[2]
    assert await (
        await pg_conn.execute(
            "SELECT status, count(*) FROM article_work WHERE company_id=%s "
            "GROUP BY status ORDER BY status",
            (company,),
        )
    ).fetchall() == [("error", threshold), ("pending", 1)]
    assert (
        await (
            await pg_conn.execute(
                "SELECT 1 FROM company_facts WHERE source_article = ANY(%s)", (article_ids,)
            )
        ).fetchone()
        is None
    )
