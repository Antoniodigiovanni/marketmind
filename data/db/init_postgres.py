"""Applies the PostgreSQL schema — the sole place that runs DDL."""

from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
from loguru import logger

from data.db.pool import dsn_from_env

POSTGRES_SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"


async def apply_schema(dsn: str) -> None:
    """Create or verify the PostgreSQL schema on its own connection. Idempotent.

    Must run before `open_pool(dsn)` against the same database — see
    `data/db/pool.py`'s module docstring for why.

    Args:
        dsn: Connection string for a role with DDL rights.

    Raises:
        FileNotFoundError: If schema_postgres.sql is missing.
        psycopg.OperationalError: If the database cannot be reached at all.
    """
    if not POSTGRES_SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {POSTGRES_SCHEMA_PATH}")

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

        ddl = POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8")
        await conn.execute(ddl)
        await conn.commit()
    finally:
        await conn.close()
    logger.debug("PostgreSQL schema applied")


def main() -> None:
    """Apply the schema against MARKETMIND_DATABASE_DSN (or its local default)."""
    dsn = dsn_from_env()
    asyncio.run(apply_schema(dsn))
    logger.info("PostgreSQL schema applied")


if __name__ == "__main__":
    main()
