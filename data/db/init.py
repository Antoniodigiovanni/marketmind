"""Database initialisation — creates research.duckdb from schema.sql."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
from loguru import logger

DB_DIR = Path(__file__).parent
SCHEMA_PATH = DB_DIR / "schema.sql"


def _default_db_path() -> Path:
    """Return the configured DuckDB path, falling back to the package data directory."""
    return Path(os.getenv("MARKETMIND_DUCKDB_PATH", DB_DIR / "research.duckdb"))


DEFAULT_DB_PATH = _default_db_path()


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    """Create/verify the DuckDB database from schema.sql. Idempotent.

    Args:
        db_path: Database file path, or ":memory:" for tests.

    Returns:
        Open connection to the database.

    Raises:
        FileNotFoundError: If schema.sql is missing.
    """
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {SCHEMA_PATH}")

    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(path)
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def main() -> None:
    """Create research.duckdb at the default location and log the table list."""
    conn = init_db()
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    logger.info(f"Database ready with {len(tables)} tables: {', '.join(t for (t,) in tables)}")
    conn.close()


if __name__ == "__main__":
    main()
