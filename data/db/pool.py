"""Async PostgreSQL connection pool with the pgvector type registered.

Side-effect-free: opening a pool creates no extension and applies no
schema. Call `apply_schema(dsn)` (see `data/db/init_postgres.py`) against a
role with DDL rights before opening a pool against the same database —
pgvector registers its type per physical connection at connect time, bound
to the `vector` extension's OID at that moment, so a pool opened first would
carry a stale registration once the extension is created.
"""

from __future__ import annotations

import os

import psycopg
from loguru import logger
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

DEFAULT_DSN = "postgresql://marketmind:marketmind@127.0.0.1:5433/marketmind_test"


def dsn_from_env() -> str:
    """Return the configured DSN, falling back to the local test default.

    MARKETMIND_DATABASE_DSN is the one variable for every environment —
    local dev, CI, and production. There is no separate prod-only name: an
    unset variable falls back to DEFAULT_DSN (the local docker-compose test
    database on 127.0.0.1:5433), which is unreachable from a real
    production host, so a missing/forgotten production DSN fails loudly as
    a connection error rather than silently succeeding against the wrong
    database.

    Returns:
        A libpq connection string.
    """
    return os.getenv("MARKETMIND_DATABASE_DSN") or DEFAULT_DSN


async def _configure(conn: psycopg.AsyncConnection) -> None:
    """Register the pgvector type on a freshly opened pool connection.

    `register_vector_async` needs the `vector` extension's type already
    present, and raises `psycopg.ProgrammingError` (a plain instance, not a
    Postgres-side error code — pgvector raises it directly) if it is not.
    That happens when a pool is opened against a database `apply_schema`
    hasn't run against yet; only that specific, narrow condition is
    swallowed here, so the pool still opens rather than failing outright. A
    connection that dies mid-configure (`psycopg.OperationalError`, a
    subclass of `psycopg.Error`) or any other unexpected failure is left to
    propagate.

    Args:
        conn: A freshly opened pool connection.
    """
    try:
        await register_vector_async(conn)
    except psycopg.ProgrammingError as exc:
        await conn.rollback()
        logger.warning(f"vector type not registered on this connection: {exc}")


async def open_pool(
    dsn: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
    open_timeout: float = 10.0,
) -> AsyncConnectionPool:
    """Open a connection pool with the vector type registered per connection.

    Connections are opened with autocommit=True, so an unwrapped repository
    read commits (or fails) as its own standalone statement rather than
    silently opening a transaction that lingers until something else closes
    it. Every repository write already scopes its own `conn.transaction()`
    block; under autocommit that block runs as a real, independent
    transaction (or, nested inside a caller's own `conn.transaction()` --
    e.g. the pg_conn test fixture -- as a savepoint, unaffected by
    autocommit). Without this, a long-lived connection held across many
    calls (e.g. one daily ingest run) turns into one giant, never-committed
    transaction the moment the first unwrapped read opens it.

    Args:
        dsn: Connection string; defaults to dsn_from_env().
        min_size: Connections kept open.
        max_size: Upper bound on concurrent connections.
        open_timeout: Seconds to wait for the pool to reach min_size.

    Returns:
        An opened AsyncConnectionPool.

    Raises:
        psycopg_pool.PoolTimeout: If the pool cannot establish its minimum
            connections within open_timeout. Side-effect-free means there is
            no earlier, faster check for an unreachable DSN — a dead DSN
            surfaces as this same timeout, not as `psycopg.OperationalError`.
            Callers on the normal path always call `apply_schema(dsn)`
            first, which does fail fast on an unreachable DSN.
    """
    resolved_dsn = dsn or dsn_from_env()
    pool = AsyncConnectionPool(
        resolved_dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True},
        configure=_configure,
        open=False,
    )
    try:
        await pool.open(wait=True, timeout=open_timeout)
    except Exception:
        # pool.open() has already started background worker tasks by the
        # time it can raise (e.g. PoolTimeout); close() stops them so a
        # failed open_pool() doesn't leak tasks that retry indefinitely.
        await pool.close()
        raise
    return pool


async def close_pool(pool: AsyncConnectionPool) -> None:
    """Close a pool opened by open_pool.

    Args:
        pool: The pool to close.
    """
    await pool.close()


async def database_available(dsn: str | None = None) -> bool:
    """Report whether a PostgreSQL server is reachable at the DSN.

    Used as a skip guard, so this is intentionally total over connection
    failures: both a live-but-unreachable server (`OperationalError`) and a
    DSN that fails to parse at all (`ProgrammingError`) are reported as
    unavailable rather than raised, so callers can rely on it never raising
    for a bad or dead DSN.

    Args:
        dsn: Connection string; defaults to dsn_from_env().

    Returns:
        True when a connection opens, False on a connection error or a
        malformed DSN.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(dsn or dsn_from_env(), connect_timeout=3)
    except (psycopg.OperationalError, psycopg.ProgrammingError) as exc:
        logger.debug(f"PostgreSQL not reachable: {exc}")
        return False
    await conn.close()
    return True
