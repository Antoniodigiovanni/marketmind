"""Unit tests for PostgreSQL migration discovery and immutability metadata."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import data.db.init_postgres as init_postgres


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple]:
        return self._rows

    async def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, *, existing_schema: bool) -> None:
        self.existing_schema = existing_schema
        self.statements: list[str] = []

    async def execute(self, statement: str, params: object = None) -> _FakeResult:
        self.statements.append(statement)
        if "FROM marketmind_meta.schema_migrations" in statement:
            return _FakeResult([])
        if "FROM information_schema.tables" in statement:
            return _FakeResult([(self.existing_schema,)])
        return _FakeResult([])

    async def commit(self) -> None:
        return None


def test_migration_files_are_ordered_and_checksummed(tmp_path: Path, monkeypatch) -> None:
    second = tmp_path / "0002_add_example.sql"
    third = tmp_path / "0003_index_example.sql"
    second.write_text("SELECT 2;\n", encoding="utf-8")
    third.write_text("SELECT 3;\n", encoding="utf-8")
    monkeypatch.setattr(init_postgres, "POSTGRES_MIGRATIONS_DIR", tmp_path)

    migrations = init_postgres._migration_files()

    assert [(version, name, path) for version, name, _, path in migrations] == [
        (2, "add_example", second),
        (3, "index_example", third),
    ]
    assert migrations[0][2] == hashlib.sha256(b"SELECT 2;\n").hexdigest()


def test_fresh_database_stamps_snapshot_migrations_but_legacy_database_runs_them(
    tmp_path: Path,
) -> None:
    migration = (2, "add_example", "checksum", tmp_path / "0002_add_example.sql")

    assert init_postgres._bootstrap_migrations([migration], existing_schema=False) == [migration]
    assert init_postgres._bootstrap_migrations([migration], existing_schema=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_schema", [False, True])
async def test_bootstrap_orders_snapshot_and_migrations_safely(
    existing_schema: bool, tmp_path: Path, monkeypatch
) -> None:
    schema_path = tmp_path / "schema.sql"
    migrations_path = tmp_path / "migrations"
    migrations_path.mkdir()
    schema_path.write_text("SELECT 'canonical-schema';", encoding="utf-8")
    (migrations_path / "0002_add_example.sql").write_text(
        "SELECT 'versioned-migration';", encoding="utf-8"
    )
    monkeypatch.setattr(init_postgres, "POSTGRES_SCHEMA_PATH", schema_path)
    monkeypatch.setattr(init_postgres, "POSTGRES_MIGRATIONS_DIR", migrations_path)
    conn = _FakeConnection(existing_schema=existing_schema)

    await init_postgres._apply_pending_migrations(conn)

    canonical_positions = [
        index for index, statement in enumerate(conn.statements) if "canonical-schema" in statement
    ]
    migration_positions = [
        index
        for index, statement in enumerate(conn.statements)
        if "versioned-migration" in statement
    ]
    if existing_schema:
        assert migration_positions[0] < canonical_positions[0]
    else:
        assert migration_positions == []
        assert len(canonical_positions) == 2


@pytest.mark.parametrize(
    "filenames",
    [
        ("invalid.sql",),
        ("0001_too_old.sql",),
        ("0003_gap.sql",),
        ("0002_one.sql", "0002_two.sql"),
    ],
)
def test_migration_files_reject_invalid_sequences(
    filenames: tuple[str, ...], tmp_path: Path, monkeypatch
) -> None:
    for filename in filenames:
        (tmp_path / filename).write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(init_postgres, "POSTGRES_MIGRATIONS_DIR", tmp_path)

    with pytest.raises(ValueError):
        init_postgres._migration_files()


def test_company_financials_migration_is_additive() -> None:
    migration = (
        Path(init_postgres.POSTGRES_MIGRATIONS_DIR) / "0004_sec_company_financials.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS financials" in migration
    assert "ALTER TABLE" not in migration
    assert "DROP " not in migration
    assert "filed_at" in migration
    assert "accession_number" in migration
    assert "NULLS NOT DISTINCT" in migration
