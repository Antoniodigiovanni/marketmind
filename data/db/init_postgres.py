"""Applies the PostgreSQL schema — the sole place that runs DDL."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import psycopg
from loguru import logger

POSTGRES_SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"
POSTGRES_MIGRATIONS_DIR = Path(__file__).parent / "postgres_migrations"
BASELINE_VERSION = 1
_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
_SCHEMA_LOCK_ID = 6_676_643_372_259_892_100


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration_files() -> list[tuple[int, str, str, Path]]:
    migrations: list[tuple[int, str, str, Path]] = []
    for path in sorted(POSTGRES_MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid PostgreSQL migration filename: {path.name}")
        version = int(match.group("version"))
        if version <= BASELINE_VERSION:
            raise ValueError(f"PostgreSQL migration version must exceed {BASELINE_VERSION}: {path}")
        migrations.append((version, match.group("name"), _checksum(path), path))

    versions = [version for version, _, _, _ in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("duplicate PostgreSQL migration version")
    if versions and versions != list(range(BASELINE_VERSION + 1, versions[-1] + 1)):
        raise ValueError("PostgreSQL migration versions must be consecutive")
    return migrations


async def _ensure_ledger(conn: psycopg.AsyncConnection) -> None:
    await conn.execute("CREATE SCHEMA IF NOT EXISTS marketmind_meta")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS marketmind_meta.schema_migrations (
            version     INTEGER     PRIMARY KEY,
            name        TEXT        NOT NULL,
            checksum    TEXT        NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.commit()


async def _applied_migrations(conn: psycopg.AsyncConnection) -> dict[int, tuple[str, str]]:
    rows = await (
        await conn.execute(
            "SELECT version, name, checksum FROM marketmind_meta.schema_migrations ORDER BY version"
        )
    ).fetchall()
    return {int(version): (str(name), str(checksum)) for version, name, checksum in rows}


async def _application_schema_exists(conn: psycopg.AsyncConnection) -> bool:
    row = await (
        await conn.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            ")"
        )
    ).fetchone()
    return row is not None and bool(row[0])


def _bootstrap_migrations(
    migrations: list[tuple[int, str, str, Path]], *, existing_schema: bool
) -> list[tuple[int, str, str, Path]]:
    # A fresh database receives the final canonical schema, so every migration
    # already represented by that snapshot must be stamped without executing.
    # A pre-ledger database has real tables in their older shape and must run
    # every migration after adopting the production baseline. The canonical
    # schema is deliberately deferred until those transitions have completed.
    return [] if existing_schema else migrations


async def _apply_pending_migrations(conn: psycopg.AsyncConnection) -> None:
    schema_sql = POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8")
    migrations = _migration_files()
    applied = await _applied_migrations(conn)

    # Test and disaster-recovery workflows may recreate `public` while the
    # migration ledger (kept in marketmind_meta) survives. Recreate the
    # canonical tables before applying any newly discovered transition.
    if applied and not await _application_schema_exists(conn):
        await conn.execute(schema_sql)
        await conn.commit()

    if not applied:
        existing_schema = await _application_schema_exists(conn)
        if not existing_schema:
            await conn.execute(schema_sql)
        await conn.execute(
            "INSERT INTO marketmind_meta.schema_migrations (version, name, checksum) "
            "VALUES (%s, %s, %s)",
            (BASELINE_VERSION, "production_baseline", "baseline"),
        )
        applied = {BASELINE_VERSION: ("production_baseline", "baseline")}

        for version, name, checksum, _ in _bootstrap_migrations(
            migrations, existing_schema=existing_schema
        ):
            await conn.execute(
                "INSERT INTO marketmind_meta.schema_migrations (version, name, checksum) "
                "VALUES (%s, %s, %s)",
                (version, name, checksum),
            )
            applied[version] = (name, checksum)
        await conn.commit()

    if applied.get(BASELINE_VERSION) != ("production_baseline", "baseline"):
        raise RuntimeError("PostgreSQL migration ledger has no valid production baseline")

    available_versions = {version for version, _, _, _ in migrations}
    unknown_versions = set(applied) - {BASELINE_VERSION} - available_versions
    if unknown_versions:
        versions = ", ".join(str(version) for version in sorted(unknown_versions))
        raise RuntimeError(f"database has migrations newer than this checkout: {versions}")

    for version, name, checksum, path in migrations:
        if version in applied:
            if applied[version] != (name, checksum):
                raise RuntimeError(f"applied PostgreSQL migration {version:04d} was modified")
            continue
        await conn.execute(path.read_text(encoding="utf-8"))
        await conn.execute(
            "INSERT INTO marketmind_meta.schema_migrations (version, name, checksum) "
            "VALUES (%s, %s, %s)",
            (version, name, checksum),
        )
        await conn.commit()
        logger.info(f"applied PostgreSQL migration {version:04d}_{name}")

    # Keep the canonical schema useful for greenfield databases and additive
    # tables/indexes. In-place changes belong in a versioned migration.
    await conn.execute(schema_sql)
    await conn.commit()


async def apply_schema(dsn: str) -> None:
    """Create or migrate the PostgreSQL schema on its own connection.

    Must run before `open_pool(dsn)` against the same database — see
    `data/db/pool.py`'s module docstring for why.

    Args:
        dsn: Connection string for a role with DDL rights.

    Raises:
        FileNotFoundError: If schema_postgres.sql is missing.
        psycopg.OperationalError: If the database cannot be reached at all.
        RuntimeError: If the migration ledger is inconsistent with this checkout.
        ValueError: If migration files are malformed or non-consecutive.
    """
    if not POSTGRES_SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {POSTGRES_SCHEMA_PATH}")
    if not POSTGRES_MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"Migration directory not found: {POSTGRES_MIGRATIONS_DIR}")

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

        await conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_ID,))
        await conn.commit()
        try:
            await _ensure_ledger(conn)
            await _apply_pending_migrations(conn)
        finally:
            await conn.rollback()
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_ID,))
            await conn.commit()
    finally:
        await conn.close()
    logger.debug("PostgreSQL schema and migrations applied")
