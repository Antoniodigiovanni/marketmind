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


async def _configure(conn: psycopg.AsyncConnection) -> None:
    """Register the pgvector type on a freshly opened pool connection.

    `register_vector_async` needs the `vector` extension's type already
    present in the database, and raises `psycopg.ProgrammingError` if it is
    not. Rather than require callers to sequence pool creation after a
    separate schema step, this ensures the extension exists on each new
    connection before registering the type. If the connecting role lacks
    the privilege to create extensions, the failure is tolerated and vector
    adaptation is skipped for that connection -- open_pool() must still
    succeed either way. A statement run outside autocommit leaves the
    connection mid-transaction, which the pool discards and retries
    forever, so both the success and failure paths finish with an explicit
    commit or rollback.
    """
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.commit()
        await register_vector_async(conn)
    except psycopg.Error as exc:
        await conn.rollback()
        logger.debug(f"vector type registration skipped: {exc}")


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
        psycopg.OperationalError: If the database cannot be reached.
    """
    pool = AsyncConnectionPool(
        dsn or dsn_from_env(),
        min_size=min_size,
        max_size=max_size,
        configure=_configure,
        open=False,
    )
    await pool.open(wait=True, timeout=10.0)
    return pool


async def close_pool(pool: AsyncConnectionPool) -> None:
    """Close a pool opened by open_pool."""
    await pool.close()


async def database_available(dsn: str | None = None) -> bool:
    """Report whether a PostgreSQL server is reachable at the DSN.

    Args:
        dsn: Connection string; defaults to dsn_from_env().

    Returns:
        True when a connection opens, False on a connection error.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(dsn or dsn_from_env(), connect_timeout=3)
    except psycopg.OperationalError as exc:
        logger.debug(f"PostgreSQL not reachable: {exc}")
        return False
    await conn.close()
    return True
