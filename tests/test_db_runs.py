"""Tests for the agent runs repository."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from data.db.runs import finish_run, start_run

pytestmark = pytest.mark.asyncio


async def test_start_run_returns_a_uuid_with_started_status(pg_conn) -> None:
    run_id = await start_run(pg_conn, run_date=dt.date(2026, 8, 11), job_type="news")
    assert isinstance(run_id, uuid.UUID)
    row = await (
        await pg_conn.execute(
            "SELECT status, finished_at FROM agent_runs WHERE run_id = %s", (run_id,)
        )
    ).fetchone()
    assert row == ("started", None)


async def test_finish_run_updates_status_counters_and_error(pg_conn) -> None:
    run_id = await start_run(pg_conn, run_date=dt.date(2026, 8, 11), job_type="news")
    await finish_run(
        pg_conn,
        run_id=run_id,
        status="succeeded",
        counters={"articles_fetched": 10, "facts_written": 3},
    )
    row = await (
        await pg_conn.execute(
            "SELECT status, counters, error, finished_at FROM agent_runs WHERE run_id = %s",
            (run_id,),
        )
    ).fetchone()
    assert row[0] == "succeeded"
    assert row[1] == {"articles_fetched": 10, "facts_written": 3}
    assert row[2] is None
    assert row[3] is not None


async def test_finish_run_records_error_on_failure(pg_conn) -> None:
    run_id = await start_run(pg_conn, run_date=dt.date(2026, 8, 11), job_type="news")
    await finish_run(pg_conn, run_id=run_id, status="failed", counters={}, error="boom")
    row = await (
        await pg_conn.execute("SELECT status, error FROM agent_runs WHERE run_id = %s", (run_id,))
    ).fetchone()
    assert row == ("failed", "boom")


async def test_finish_run_rejects_invalid_status(pg_conn) -> None:
    run_id = await start_run(pg_conn, run_date=dt.date(2026, 8, 11), job_type="news")
    with pytest.raises(ValueError, match="status must be one of"):
        await finish_run(pg_conn, run_id=run_id, status="not-a-status", counters={})
    row = await (
        await pg_conn.execute("SELECT status FROM agent_runs WHERE run_id = %s", (run_id,))
    ).fetchone()
    assert row[0] == "started"  # untouched
