"""Shared helpers for the Postgres-backed test suite."""

from __future__ import annotations

import os

import pytest


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
