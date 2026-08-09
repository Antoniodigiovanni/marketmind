"""Async PostgreSQL connection pool with the pgvector type registered.

Transitional: DuckDB remains the store while the migration is in progress.
Nothing in the application reads this module yet.
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

    Returns:
        A libpq connection string.
    """
    return os.getenv("MARKETMIND_TEST_DSN") or DEFAULT_DSN


async def _ensure_vector_extension(dsn: str) -> None:
    """Create the `vector` extension once, before any pool connection opens.

    `CREATE EXTENSION IF NOT EXISTS` is not safe under concurrent execution:
    a losing session can hit a duplicate-object error even though the
    extension ends up present. Running it exactly once, on a single
    throwaway connection, before the pool starts opening its own (which can
    happen concurrently once demand exceeds ``min_size``) avoids that race
    *within this process*. A second process racing this same call against
    the same database is a separate, cross-process window this does not
    close: only `InsufficientPrivilege` is tolerated below, so a losing
    session's `DuplicateObject` would still propagate. That window is
    unreachable today (one service process, one pytest process, no
    pytest-xdist) and is tracked as follow-up work rather than closed here.

    Args:
        dsn: Connection string to create the extension on.

    Raises:
        psycopg.OperationalError: If the database cannot be reached at all.
    """
    conn = await psycopg.AsyncConnection.connect(dsn, connect_timeout=3)
    try:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.commit()
        except psycopg.errors.InsufficientPrivilege as exc:
            await conn.rollback()
            logger.warning(
                "role lacks privilege to create the vector extension; "
                f"proceeding on the assumption it already exists: {exc}"
            )
    finally:
        await conn.close()


async def _configure(conn: psycopg.AsyncConnection) -> None:
    """Register the pgvector type on a freshly opened pool connection.

    `register_vector_async` needs the `vector` extension's type already
    present, and raises `psycopg.ProgrammingError` (a plain instance, not a
    Postgres-side error code -- pgvector raises it directly) if it is not.
    `open_pool` already attempts to create the extension once up front, but
    that attempt may itself have been skipped on privilege grounds, so this
    still tolerates the type being absent rather than failing the whole
    pool. Only that specific, narrow condition is swallowed: a connection
    that dies mid-configure (`psycopg.OperationalError`, a subclass of
    `psycopg.Error`) or any other unexpected failure is left to propagate.
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
) -> AsyncConnectionPool:
    """Open a connection pool with the vector type registered per connection.

    Args:
        dsn: Connection string; defaults to dsn_from_env().
        min_size: Connections kept open.
        max_size: Upper bound on concurrent connections.

    Returns:
        An opened AsyncConnectionPool.

    Raises:
        psycopg.OperationalError: If the database cannot be reached while
            creating the vector extension, before any pool connection opens.
        psycopg_pool.PoolTimeout: If the pool cannot establish its minimum
            connections within its internal open timeout (10 seconds) even
            though `_ensure_vector_extension` above already reached the
            server -- for example because the server is at its own
            connection limit and keeps refusing new ones. A DSN that is
            simply unreachable is caught earlier, by
            `_ensure_vector_extension`, and surfaces as
            `psycopg.OperationalError` instead; this timeout is for the
            pool's own connection establishment stalling after that check
            already succeeded.
    """
    resolved_dsn = dsn or dsn_from_env()
    await _ensure_vector_extension(resolved_dsn)

    pool = AsyncConnectionPool(
        resolved_dsn,
        min_size=min_size,
        max_size=max_size,
        configure=_configure,
        open=False,
    )
    try:
        await pool.open(wait=True, timeout=10.0)
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
