"""Committed-state regression for interrupted article extraction and feed-free replay."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid

import polars as pl
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import agent.ingestion as ingestion
from data.db.init_postgres import apply_schema
from data.db.pool import close_pool, database_available, dsn_from_env, open_pool
from data.sources.rss import Article
from tests.conftest import skip_or_fail_no_postgres


@pytest.mark.parametrize("failure_site", ["second_fact", "embedding", "status"])
@pytest.mark.parametrize("replay_status", ["pending", "error"])
@pytest.mark.asyncio
async def test_second_fact_failure_is_atomic_and_replayed_without_feed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    replay_status: str,
    failure_site: str,
) -> None:
    """A failed pair commits no facts and finishes from durable work on a later run."""
    dsn = dsn_from_env()
    if not (conninfo_to_dict(dsn).get("dbname") or "").endswith("_test"):
        skip_or_fail_no_postgres(
            "refusing article recovery writes outside a disposable _test database"
        )
    if not await database_available(dsn):
        skip_or_fail_no_postgres("no PostgreSQL reachable for committed article recovery tests")
    # Deliberately avoid pg_conn: its outer rollback hides partial production commits.
    await apply_schema(dsn)
    pool = await open_pool(dsn)
    token = uuid.uuid4().hex
    ticker = f"REC{token}"
    date = "2026-08-04"
    universe = pl.DataFrame(
        {
            "ticker": [ticker],
            "name": [f"Recovery {token}"],
            "country": ["US"],
            "sector": [f"Recovery {token}"],
            "added_date": [dt.date(2020, 1, 1)],
            "removed_date": [None],
            "universe": ["sp500"],
        }
    )
    article = Article(
        ticker=ticker,
        url=f"https://example.invalid/{token}",
        title="Recovery report",
        summary="Two distinct developments.",
        source="google-news",
        published_at="2026-08-04T09:00:00+00:00",
    )
    statements = [f"First development {token}", f"Second development {token}"]
    calls = 0
    real_embed = ingestion.hash_embed
    real_status = ingestion.set_article_status
    real_create_fact = ingestion.create_fact
    run_ids = []
    real_start_run = ingestion.start_run

    old_fact = None
    real_ensure = ingestion._ensure_company

    async def ensure_with_prior_fact(*args, **kwargs):
        nonlocal old_fact
        result = await real_ensure(*args, **kwargs)
        if old_fact is None:
            old_fact = await real_create_fact(
                args[0],
                company_id=result[0],
                statement=f"Prior development {token}",
                fact_type="general",
                confidence=0.8,
                valid_from=dt.date(2026, 8, 3),
                source_article_id=None,
                embed=real_embed,
                model="hash-ngram",
            )
        return result

    monkeypatch.setattr(ingestion, "_ensure_company", ensure_with_prior_fact)

    async def track_run(*args, **kwargs):
        run_id = await real_start_run(*args, **kwargs)
        run_ids.append(run_id)
        return run_id

    async def fail_second_fact(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and failure_site == "second_fact":
            raise RuntimeError("injected second-fact failure")
        return await real_create_fact(*args, **kwargs)

    def llm(messages: list[dict[str, str]]) -> str:
        if "aggregate" in messages[-1]["content"]:
            return "REDUNDANT"
        return json.dumps(
            [
                {
                    "statement": statement,
                    "fact_type": "general",
                    "confidence": 0.8,
                    "supersedes_statement": f"Prior development {token}"
                    if statement == statements[0]
                    else "",
                }
                for statement in statements
            ]
        )

    async def fail_embedding(text):
        if failure_site == "embedding" and calls == 2:
            raise RuntimeError("injected embedding failure")
        return await real_embed(text)

    async def fail_status(*args, **kwargs):
        await real_status(*args, **kwargs)
        if failure_site == "status" and kwargs["status"] == "processed":
            raise RuntimeError("injected status failure")

    monkeypatch.setattr(ingestion, "hash_embed", fail_embedding)
    monkeypatch.setattr(ingestion, "set_article_status", fail_status)
    monkeypatch.setattr(ingestion, "create_fact", fail_second_fact)
    monkeypatch.setattr(ingestion, "start_run", track_run)
    options = dict(
        universe=universe,
        description_fetcher=lambda name: "Test company.",
        profile_builder=lambda name: {
            "description": "Test company.",
            "products_services": [],
            "generated_by": "test",
        },
        llm=llm,
        raw_news_dir=tmp_path,
    )
    try:
        async with pool.connection() as writer:
            assert writer.autocommit
            first = await ingestion.run_daily_ingest(
                writer,
                date,
                news_fetcher=lambda ticker, name: [article],
                **options,
            )
        assert calls == 2
        assert first["articles_errored"] == 1
        # Fresh physical connection: only committed rows can be seen here.
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            pair = await (
                await observer.execute(
                    "SELECT a.article_id, ac.company_id, ac.status FROM articles a "
                    "JOIN article_companies ac USING (article_id) WHERE a.url_hash = %s",
                    (article.url_hash,),
                )
            ).fetchone()
            assert pair is not None
            failed_facts = await (
                await observer.execute(
                    "SELECT statement FROM company_facts WHERE source_article = %s",
                    (pair[0],),
                )
            ).fetchall()
        async with pool.connection() as writer:
            await writer.execute(
                "UPDATE article_companies SET status = %s "
                "WHERE article_id = %s AND company_id = %s",
                (replay_status, *pair[:2]),
            )
        assert pair[2] == "error"
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            state = await (
                await observer.execute(
                    "SELECT status FROM article_work WHERE article_id=%s AND company_id=%s",
                    pair[:2],
                )
            ).fetchone()
            assert state == ("error",)
            assert await (
                await observer.execute(
                    "SELECT valid_until FROM company_facts WHERE fact_id=%s", (old_fact,)
                )
            ).fetchone() == (None,)
            for table in ("fact_embeddings", "article_fact_inputs"):
                rows = await (
                    await observer.execute(
                        f"SELECT count(*) FROM {table} x JOIN company_facts f USING(fact_id) "
                        "WHERE f.source_article=%s",
                        (pair[0],),
                    )
                ).fetchone()
                assert rows == (0,)
        monkeypatch.setattr(ingestion, "hash_embed", real_embed)
        monkeypatch.setattr(ingestion, "set_article_status", real_status)
        monkeypatch.setattr(ingestion, "create_fact", real_create_fact)

        async def replay():
            async with pool.connection() as retry_writer:
                return await ingestion.run_daily_ingest(
                    retry_writer,
                    date,
                    news_fetcher=lambda ticker, name: [],
                    **{**options, "universe": universe.head(0)},
                )

        await asyncio.wait_for(asyncio.gather(replay(), replay()), timeout=30)
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            replay_status = await (
                await observer.execute(
                    "SELECT status FROM article_companies WHERE article_id = %s AND company_id = %s",
                    pair[:2],
                )
            ).fetchone()
            replay_facts = await (
                await observer.execute(
                    "SELECT statement FROM company_facts WHERE source_article = %s ORDER BY statement",
                    (pair[0],),
                )
            ).fetchall()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            assert await (
                await observer.execute(
                    "SELECT valid_until FROM company_facts WHERE fact_id=%s", (old_fact,)
                )
            ).fetchone() == (dt.date.fromisoformat(date),)
        observed = {
            "facts_after_failure": failed_facts,
            "status_after_replay": replay_status,
            "facts_after_replay": replay_facts,
        }
        assert observed == {
            "facts_after_failure": [],
            "status_after_replay": ("processed",),
            "facts_after_replay": sorted((statement,) for statement in statements),
        }, f"committed article state is not recoverable: {observed!r}"
    finally:
        async with pool.connection() as cleanup:
            await cleanup.execute(
                "DELETE FROM companies WHERE name = %s",
                (f"Recovery {token}",),
            )
            await cleanup.execute("DELETE FROM articles WHERE url_hash = %s", (article.url_hash,))
            for run_id in run_ids:
                await cleanup.execute("DELETE FROM agent_runs WHERE run_id = %s", (run_id,))
        await close_pool(pool)


@pytest.mark.parametrize("fail_at", [2, 4])
@pytest.mark.asyncio
async def test_completed_article_aggregate_restart_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    fail_at: int,
) -> None:
    """Completed articles retain aggregate inputs after a failed group and restart."""
    dsn = dsn_from_env()
    if not (conninfo_to_dict(dsn).get("dbname") or "").endswith("_test"):
        skip_or_fail_no_postgres(
            "refusing article recovery writes outside a disposable _test database"
        )
    if not await database_available(dsn):
        skip_or_fail_no_postgres("no PostgreSQL reachable for committed article recovery tests")
    # Deliberately avoid pg_conn: its outer rollback hides partial production commits.
    await apply_schema(dsn)
    pool = await open_pool(dsn)
    token = uuid.uuid4().hex
    ticker = f"REC{token}"
    date = "2026-08-04"
    universe = pl.DataFrame(
        {
            "ticker": [ticker],
            "name": [f"Recovery {token}"],
            "country": [f"Country {token}"],
            "sector": [f"Recovery {token}"],
            "added_date": [dt.date(2020, 1, 1)],
            "removed_date": [None],
            "universe": ["sp500"],
        }
    )
    article = Article(
        ticker=ticker,
        url=f"https://example.invalid/{token}",
        title="Recovery report",
        summary="Two distinct developments.",
        source="google-news",
        published_at="2026-08-04T09:00:00+00:00",
    )
    statements = [f"First development {token}", f"Second development {token}"]
    calls = 0
    real_create_fact = ingestion.create_aggregate_fact
    run_ids = []
    real_start_run = ingestion.start_run

    async def track_run(*args, **kwargs):
        run_id = await real_start_run(*args, **kwargs)
        run_ids.append(run_id)
        return run_id

    async def fail_second_fact(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise RuntimeError("injected second-fact failure")
        return await real_create_fact(*args, **kwargs)

    def llm(messages: list[dict[str, str]]) -> str:
        if "aggregate" in messages[-1]["content"]:
            return json.dumps(
                [{"statement": f"Aggregate {i} {token}", "confidence": 0.8} for i in range(2)]
            )
        return json.dumps(
            [
                {"statement": statement, "fact_type": "general", "confidence": 0.8}
                for statement in statements
            ]
        )

    monkeypatch.setattr(ingestion, "create_aggregate_fact", fail_second_fact)
    monkeypatch.setattr(ingestion, "start_run", track_run)
    options = dict(
        universe=universe,
        description_fetcher=lambda name: "Test company.",
        profile_builder=lambda name: {
            "description": "Test company.",
            "products_services": [],
            "generated_by": "test",
        },
        llm=llm,
        raw_news_dir=tmp_path,
    )
    try:
        # A failure may propagate or be recorded per group; both must leave
        # only complete committed groups visible to a separate connection.
        async with pool.connection() as writer:
            try:
                await ingestion.run_daily_ingest(
                    writer, date, news_fetcher=lambda ticker, name: [article], **options
                )
            except RuntimeError as exc:
                assert str(exc) == "injected second-fact failure"
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            pair = await (
                await observer.execute(
                    "SELECT ac.status FROM articles a JOIN article_companies ac "
                    "USING (article_id) WHERE a.url_hash = %s",
                    (article.url_hash,),
                )
            ).fetchone()
            partial = await (
                await observer.execute(
                    "SELECT statement FROM aggregate_facts "
                    "WHERE dimension = 'sector' AND dimension_key = %s",
                    (f"Recovery {token}",),
                )
            ).fetchall()
        assert pair == ("processed",)
        expected = [] if fail_at == 2 else [(f"Aggregate {i} {token}",) for i in range(2)]
        assert sorted(partial) == expected, "a failed aggregate group committed partial output"
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            for table in ("article_aggregate_work", "article_aggregate_inputs"):
                count = await (
                    await observer.execute(
                        f"SELECT count(*) FROM {table} WHERE dimension='sector' AND dimension_key=%s",
                        (f"Recovery {token}",),
                    )
                ).fetchone()
                assert count == ((0 if fail_at == 2 else (1 if table.endswith("work") else 2)),)
        monkeypatch.setattr(ingestion, "create_aggregate_fact", real_create_fact)
        for _ in range(2):
            async with pool.connection() as writer:
                await ingestion.run_daily_ingest(
                    writer, date, news_fetcher=lambda ticker, name: [], **options
                )
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            outputs = await (
                await observer.execute(
                    "SELECT statement FROM aggregate_facts "
                    "WHERE dimension = 'sector' AND dimension_key = %s ORDER BY statement",
                    (f"Recovery {token}",),
                )
            ).fetchall()
        assert outputs == [(f"Aggregate {i} {token}",) for i in range(2)]
        # A later batch on the same date must consume only newly arriving facts.
        second = Article(
            ticker=ticker,
            url=article.url + "/later",
            title="Later report",
            summary="New developments",
            source="google-news",
            published_at=article.published_at,
        )
        async with pool.connection() as writer:
            await ingestion.run_daily_ingest(
                writer, date, news_fetcher=lambda *_: [second], **options
            )
            await ingestion.run_daily_ingest(writer, date, news_fetcher=lambda *_: [], **options)
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer:
            count = await (
                await observer.execute(
                    "SELECT count(*), count(DISTINCT evidence_hash) FROM article_aggregate_inputs "
                    "WHERE dimension='sector' AND dimension_key=%s",
                    (f"Recovery {token}",),
                )
            ).fetchone()
            assert count == (4, 2)
            count = await (
                await observer.execute(
                    "SELECT count(*) FROM aggregate_facts WHERE dimension='sector' AND dimension_key=%s",
                    (f"Recovery {token}",),
                )
            ).fetchone()
            assert count == (4,)
    finally:
        async with pool.connection() as cleanup:
            await cleanup.execute(
                "DELETE FROM aggregate_facts WHERE statement LIKE %s", (f"%{token}",)
            )
            await cleanup.execute(
                "DELETE FROM companies WHERE name = %s",
                (f"Recovery {token}",),
            )
            await cleanup.execute("DELETE FROM articles WHERE url_hash = %s", (article.url_hash,))
            await cleanup.execute("DELETE FROM articles WHERE url=%s", (article.url + "/later",))
            await cleanup.execute(
                "DELETE FROM article_aggregate_work WHERE dimension_key=%s",
                (f"Recovery {token}",),
            )
            # Geography completion records carry no company FK; remove only this run's batches.
            await cleanup.execute(
                "DELETE FROM article_aggregate_work WHERE dimension_key=%s", (f"Country {token}",)
            )
            for run_id in run_ids:
                await cleanup.execute("DELETE FROM agent_runs WHERE run_id = %s", (run_id,))
        await close_pool(pool)


async def _wait_for_blocker(observer, waiter_pid: int, blocker_pid: int) -> None:
    """Require observed PostgreSQL contention rather than task scheduling alone."""
    async with asyncio.timeout(5):
        while True:
            row = await (
                await observer.execute("SELECT pg_blocking_pids(%s)", (waiter_pid,))
            ).fetchone()
            if blocker_pid in row[0]:
                return
            await asyncio.sleep(0.01)


@pytest.fixture
async def local_lock_pool():
    """Keep pool workers and replacement connections on this test's event loop."""
    dsn = dsn_from_env()
    if not (conninfo_to_dict(dsn).get("dbname") or "").endswith("_test"):
        skip_or_fail_no_postgres("refusing lock tests outside a disposable _test database")
    if not await database_available(dsn):
        skip_or_fail_no_postgres("no PostgreSQL reachable for lock tests")
    pool = await open_pool(dsn, min_size=2)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["success", "exception", "cancel_body", "cancel_wait"])
async def test_physical_session_contention_and_release(local_lock_pool, interruption):
    """Interrupted lock owners and waiters leave pooled physical sessions reusable."""
    dsn = dsn_from_env()
    async with (
        local_lock_pool.connection() as owner,
        local_lock_pool.connection() as waiter,
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as observer,
    ):
        owner_pid = (await (await owner.execute("SELECT pg_backend_pid()")).fetchone())[0]
        waiter_pid = (await (await waiter.execute("SELECT pg_backend_pid()")).fetchone())[0]
        observer_pid = (await (await observer.execute("SELECT pg_backend_pid()")).fetchone())[0]
        assert len({owner_pid, waiter_pid, observer_pid}) == 3
        entered = asyncio.Event()
        release = asyncio.Event()

        @ingestion._serialized_ingest
        async def hold(conn):
            entered.set()
            await release.wait()
            if interruption == "exception":
                raise RuntimeError("injected owner failure")

        @ingestion._serialized_ingest
        async def acquire(conn):
            return "acquired"

        first = asyncio.create_task(hold(owner))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(acquire(waiter))
            await _wait_for_blocker(observer, waiter_pid, owner_pid)
            if interruption == "cancel_wait":
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(second, 5)
            if interruption == "cancel_body":
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(first, 5)
            else:
                release.set()
                if interruption == "exception":
                    with pytest.raises(RuntimeError, match="injected owner failure"):
                        await asyncio.wait_for(first, 5)
                else:
                    await asyncio.wait_for(first, 5)
            if interruption != "cancel_wait":
                assert await asyncio.wait_for(second, 5) == "acquired"
            # Reuse the same physical sessions; closing them would hide a leaked lock.
            assert (await (await owner.execute("SELECT pg_backend_pid()")).fetchone())[
                0
            ] == owner_pid
            assert (await (await waiter.execute("SELECT pg_backend_pid()")).fetchone())[
                0
            ] == waiter_pid
            assert await asyncio.wait_for(acquire(owner), 5) == "acquired"
            assert await asyncio.wait_for(acquire(waiter), 5) == "acquired"
            row = await (
                await observer.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid = ANY(%s)",
                    ([owner_pid, waiter_pid],),
                )
            ).fetchone()
            assert row == (0,)
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (first, second) if task), return_exceptions=True
            )


@pytest.mark.asyncio
async def test_cancellation_during_unlock_releases_physical_session(local_lock_pool):
    """Cancellation during shielded cleanup must release the server session lock."""
    unlocking = asyncio.Event()
    proceed = asyncio.Event()
    async with local_lock_pool.connection() as physical:
        pid = (await (await physical.execute("SELECT pg_backend_pid()")).fetchone())[0]

        class DelayedUnlock:
            async def execute(self, sql):
                if "pg_advisory_unlock" in sql:
                    unlocking.set()
                    await proceed.wait()
                    if physical.closed:
                        return None
                return await physical.execute(sql)

            async def close(self):
                await physical.close()

        @ingestion._serialized_ingest
        async def finish(conn):
            return None

        task = asyncio.create_task(finish(DelayedUnlock()))
        try:
            await asyncio.wait_for(unlocking.wait(), 5)
            async with await psycopg.AsyncConnection.connect(
                dsn_from_env(), autocommit=True
            ) as observer:
                row = await (
                    await observer.execute(
                        "SELECT count(*) FROM pg_locks WHERE pid=%s AND locktype='advisory'", (pid,)
                    )
                ).fetchone()
                assert row == (1,)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                proceed.set()
                async with asyncio.timeout(5):
                    while True:
                        row = await (
                            await observer.execute(
                                "SELECT count(*) FROM pg_locks WHERE pid=%s AND locktype='advisory'",
                                (pid,),
                            )
                        ).fetchone()
                        if row == (0,):
                            break
                        await asyncio.sleep(0.01)
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with local_lock_pool.connection() as reused:
        assert await (
            await reused.execute("SELECT pg_try_advisory_lock(731942617028)")
        ).fetchone() == (True,)
        await reused.execute("SELECT pg_advisory_unlock(731942617028)")
