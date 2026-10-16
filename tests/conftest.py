"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from data.db.init_postgres import apply_schema
from data.db.pool import close_pool, database_available, dsn_from_env, open_pool


@pytest.fixture
def isolated_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate opted-in job tests and finish their worker before restoring patches."""
    import agent.refresh as refresh
    import server.jobs as jobs

    monkeypatch.setenv(refresh.REFRESH_DIR_ENV, str(tmp_path / "refresh"))
    refresh.refresh_dir().mkdir(parents=True)
    monkeypatch.delenv(refresh.INHERITED_LOCK_FD_ENV, raising=False)
    monkeypatch.setattr(jobs, "_CURRENT", None)
    try:
        yield
    finally:
        record = jobs.current_run()
        if record is not None and record.thread is not None and record.thread.ident is not None:
            record.thread.join()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip live contract tests unless the caller explicitly enables them."""
    if os.getenv("RUN_LIVE_TESTS") == "1":
        return
    marker = pytest.mark.skip(reason="set RUN_LIVE_TESTS=1 to call live external services")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(marker)


@pytest.fixture(autouse=True)
def _deterministic_operator_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fixed operator contact for courtesy-access sources.

    Two reasons this is autouse. Tests must never send a developer's real
    address at a live endpoint, and they must not start failing on a machine
    where SEC_CONTACT_* happens to be unset. Tests that exercise the missing
    or malformed cases override this with their own `monkeypatch.delenv` or
    `setenv`, which runs later and therefore wins.

    `.invalid` is reserved by RFC 2606 and resolves nowhere, so a leaked
    request is inert.
    """
    if os.getenv("RUN_LIVE_TESTS") != "1":
        monkeypatch.setenv("SEC_CONTACT_NAME", "marketmind test suite")
        monkeypatch.setenv("SEC_CONTACT_EMAIL", "tests@marketmind.invalid")


def skip_or_fail_no_postgres(message: str) -> None:
    """Skip a test, or fail it when PostgreSQL is required.

    Postgres-backed tests treat an unreachable or refused database as a
    skip by default, so contributors without Docker still get a green
    local run. Setting `MARKETMIND_REQUIRE_POSTGRES=1` (as CI does) turns
    that skip into a hard failure instead, so a broken connection, a wrong
    port mapping, or a DSN pointed at the wrong database cannot pass
    silently -- pgvector validation either runs or the build fails.

    Args:
        message: Reason to report, via `pytest.skip` or `pytest.fail`.

    Raises:
        Exception: `pytest.skip`'s or `pytest.fail`'s internal control-flow
            exception, per the usual pytest skip/fail mechanics.
    """
    if os.getenv("MARKETMIND_REQUIRE_POSTGRES") == "1":
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
async def postgres_pool():
    """Session-scoped pool: schema applied once, torn down once at session end.

    Skips (or fails, under MARKETMIND_REQUIRE_POSTGRES=1) when no reachable
    test database is configured. Refuses to run against a database whose
    name doesn't end in '_test', since this drops and recreates the
    `public` schema and the migration ledger before applying — safe only
    against a disposable database.
    """
    if not await database_available():
        skip_or_fail_no_postgres("no PostgreSQL reachable; start it with `docker compose up -d`")

    dsn = dsn_from_env()
    dbname = conninfo_to_dict(dsn).get("dbname") or ""
    if not dbname.endswith("_test"):
        skip_or_fail_no_postgres(
            f"refusing to reset database {dbname!r}: name does not end in '_test'"
        )

    reset_conn = await psycopg.AsyncConnection.connect(dsn, connect_timeout=3)
    try:
        await reset_conn.execute(
            "DROP SCHEMA IF EXISTS marketmind_meta CASCADE; "
            "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
        )
        await reset_conn.commit()
    finally:
        await reset_conn.close()

    await apply_schema(dsn)
    pool = await open_pool(dsn)
    yield pool
    await close_pool(pool)


@pytest.fixture
async def pg_conn(postgres_pool):
    """One pooled connection per test, in a transaction rolled back at teardown.

    Any repository function called with this connection during a test must
    scope its own statement groups with `async with conn.transaction():`
    rather than calling `conn.commit()` directly — psycopg nests an inner
    `transaction()` opened while one is already active as a SAVEPOINT, so
    this fixture's rollback undoes everything a repository function wrote,
    even though the function itself believes it committed its own work.
    """
    async with postgres_pool.connection() as conn, conn.transaction() as tx:
        yield conn
        raise psycopg.Rollback(tx)
