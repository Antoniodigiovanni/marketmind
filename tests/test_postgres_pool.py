"""Tests for the async PostgreSQL connection pool."""

from __future__ import annotations

import asyncio

import pytest
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from data.db.pool import close_pool, database_available, dsn_from_env, open_pool
from tests.conftest import skip_or_fail_no_postgres

pytestmark = pytest.mark.asyncio


async def _skip_if_unavailable() -> None:
    if not await database_available():
        skip_or_fail_no_postgres("no PostgreSQL reachable; start it with `docker compose up -d`")


async def test_dsn_prefers_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("MARKETMIND_TEST_DSN", "postgresql://x@example/y")
    assert dsn_from_env() == "postgresql://x@example/y"


async def test_dsn_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.delenv("MARKETMIND_TEST_DSN", raising=False)
    assert dsn_from_env().startswith("postgresql://")


async def test_pool_executes_a_query() -> None:
    await _skip_if_unavailable()
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT 1")).fetchone()
        assert row == (1,)
    finally:
        await close_pool(pool)


async def test_database_available_is_false_for_a_dead_dsn() -> None:
    assert await database_available("postgresql://nobody@127.0.0.1:1/nothing") is False


async def test_pool_registers_the_vector_type() -> None:
    """A registered vector type round-trips as a pgvector Vector, not a string.

    pgvector 0.5.0's psycopg loader returns its own `Vector` wrapper (not a
    plain list/ndarray, and not iterable via `list()`); `.to_list()` is the
    documented way to get plain floats back out.
    """
    await _skip_if_unavailable()
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT '[1,2,3]'::vector")).fetchone()
        assert not isinstance(row[0], str)
        assert row[0].to_list() == [1.0, 2.0, 3.0]
    finally:
        await close_pool(pool)


async def test_pool_registers_the_vector_type_for_concurrently_opened_connections() -> None:
    """Every connection opened concurrently must get the vector type registered.

    Regression coverage for the fix to Finding 2, not a reproduction of the
    original bug: before the fix, `CREATE EXTENSION IF NOT EXISTS` ran
    inside `_configure` on every physical connection. That statement is not
    race-free under concurrent execution, so a losing connection could hit
    a duplicate-object error and silently end up without vector support.
    That race is inherently timing-dependent -- it cannot be forced
    deterministically without mocking psycopg internals, which would test
    the mock rather than real behaviour, so this does not attempt to
    reproduce it. It instead exercises the fixed design (the extension is
    created exactly once, before the pool opens any connection) by forcing
    several connections open at once and checking that all of them --  not
    just the first -- got a working vector type back, not a bare string.
    Verified manually against a genuinely fresh database (extension
    dropped, then this same pattern run directly) as part of the fix; see
    the task report.
    """
    await _skip_if_unavailable()
    pool = await open_pool(min_size=1, max_size=5)
    try:

        async def query() -> object:
            async with pool.connection() as conn:
                row = await (await conn.execute("SELECT '[1,2,3]'::vector")).fetchone()
                return row[0]

        results = await asyncio.gather(*(query() for _ in range(8)))
        for value in results:
            assert not isinstance(value, str)
            assert value.to_list() == [1.0, 2.0, 3.0]
    finally:
        await close_pool(pool)


async def test_open_pool_leaves_no_running_tasks_for_an_unreachable_dsn() -> None:
    """A failed open_pool() against a dead DSN must not leak background tasks.

    open_pool is side-effect-free (Task 1): it has no pre-flight reachability
    check of its own, so a dead DSN surfaces as the pool's own PoolTimeout
    rather than an immediate OperationalError. open_timeout is set low here
    so the test doesn't wait out the 10s production default.
    """
    current = asyncio.current_task()
    tasks_before = {t for t in asyncio.all_tasks() if t is not current}

    with pytest.raises(PoolTimeout):
        await open_pool("postgresql://nobody@127.0.0.1:1/nothing", open_timeout=1.0)

    tasks_after = {t for t in asyncio.all_tasks() if t is not current}
    assert tasks_after - tasks_before == set(), "open_pool leaked background tasks"


async def test_open_pool_closes_the_pool_when_pool_open_itself_fails(monkeypatch) -> None:
    """open_pool() must close the pool before re-raising a pool.open() failure.

    Forces the failure inside `AsyncConnectionPool.open` directly -- after
    a real pool object exists -- to deterministically exercise the specific
    branch this finding is about, rather than relying on a DSN that happens
    to fail early (see the test above, which fails before a pool object is
    ever created).
    """
    await _skip_if_unavailable()

    closed_pools = []
    original_close = AsyncConnectionPool.close

    async def failing_open(self, *args, **kwargs):
        raise RuntimeError("simulated pool.open() failure")

    async def tracking_close(self, *args, **kwargs):
        closed_pools.append(self)
        return await original_close(self, *args, **kwargs)

    monkeypatch.setattr(AsyncConnectionPool, "open", failing_open)
    monkeypatch.setattr(AsyncConnectionPool, "close", tracking_close)

    with pytest.raises(RuntimeError, match="simulated pool.open"):
        await open_pool()

    assert len(closed_pools) == 1
