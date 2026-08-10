"""Proves the pg_conn fixture's per-test rollback, independent of any
repository module (none exist yet) — the mechanism itself must be verified
before later phases build on it."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

_COMPANY_ID = "77777777-7777-7777-7777-777777777777"


async def test_write_via_nested_transaction_is_visible_within_the_test(pg_conn) -> None:
    """A write made through its own nested `transaction()` block — the
    pattern real repository functions will use — is visible to later
    statements in the same test."""
    async with pg_conn.transaction():
        await pg_conn.execute(
            "INSERT INTO companies (company_id, name, first_seen) "
            "VALUES (%s, 'RollbackFixtureCo', '2020-01-01')",
            (_COMPANY_ID,),
        )
    row = await (
        await pg_conn.execute("SELECT name FROM companies WHERE company_id = %s", (_COMPANY_ID,))
    ).fetchone()
    assert row == ("RollbackFixtureCo",)


async def test_previous_test_write_did_not_survive(pg_conn) -> None:
    """Confirms test isolation: the row inserted (and committed, from the
    nested transaction's own point of view) in the previous test must not
    be visible here — proving the fixture's outer rollback undid it."""
    row = await (
        await pg_conn.execute("SELECT name FROM companies WHERE company_id = %s", (_COMPANY_ID,))
    ).fetchone()
    assert row is None
