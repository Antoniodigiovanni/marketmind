"""Tests for the async PostgreSQL connection pool."""

from __future__ import annotations

import pytest

from data.db.postgres import close_pool, database_available, dsn_from_env, open_pool

pytestmark = pytest.mark.asyncio


async def _skip_if_unavailable() -> None:
    if not await database_available():
        pytest.skip("no PostgreSQL reachable; start it with `docker compose up -d`")


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
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            row = await (await conn.execute("SELECT '[1,2,3]'::vector")).fetchone()
        assert not isinstance(row[0], str)
        assert row[0].to_list() == [1.0, 2.0, 3.0]
    finally:
        await close_pool(pool)
