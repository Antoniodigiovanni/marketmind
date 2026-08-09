"""Applies the PostgreSQL schema. Transitional -- see data/db/init.py for DuckDB."""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from psycopg_pool import AsyncConnectionPool

POSTGRES_SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"


async def apply_schema(pool: AsyncConnectionPool) -> None:
    """Create or verify the PostgreSQL schema. Idempotent.

    Args:
        pool: An open connection pool.

    Raises:
        FileNotFoundError: If schema_postgres.sql is missing.
    """
    if not POSTGRES_SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {POSTGRES_SCHEMA_PATH}")

    ddl = POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.connection() as conn:
        await conn.execute(ddl)
    logger.debug("PostgreSQL schema applied")
