"""Tests for the agent runs repository."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from data.db.runs import finish_run, list_recent_runs, start_run

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


async def test_list_recent_runs_orders_newest_first_and_returns_nested_counters(pg_conn) -> None:
    first = await start_run(pg_conn, run_date=dt.date(2026, 8, 11), job_type="news")
    await finish_run(pg_conn, run_id=first, status="succeeded", counters={"facts_written": 1})
    second = await start_run(pg_conn, run_date=dt.date(2026, 8, 12), job_type="news")
    await finish_run(pg_conn, run_id=second, status="succeeded", counters={"facts_written": 2})

    # NOW() is frozen for the whole transaction (pg_conn wraps each test in
    # one outer transaction, and start_run's own transaction is a nested
    # savepoint, not a fresh one) -- both runs would otherwise share the
    # same started_at. Set them explicitly so the ordering assertion below
    # is actually testing ORDER BY, not a tie-break coin flip.
    await pg_conn.execute(
        "UPDATE agent_runs SET started_at = %s WHERE run_id = %s",
        (dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.UTC), first),
    )
    await pg_conn.execute(
        "UPDATE agent_runs SET started_at = %s WHERE run_id = %s",
        (dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.UTC), second),
    )

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert [r["run_id"] for r in runs] == [second, first]
    assert runs[0]["counters"] == {"facts_written": 2}


async def test_list_recent_runs_filters_by_job_type(pg_conn) -> None:
    news_run = await start_run(pg_conn, run_date=dt.date(2026, 8, 11), job_type="news")
    await finish_run(pg_conn, run_id=news_run, status="succeeded", counters={})
    filings_run = await start_run(pg_conn, run_date=dt.date(2026, 8, 11), job_type="filings")
    await finish_run(pg_conn, run_id=filings_run, status="succeeded", counters={})

    runs = await list_recent_runs(pg_conn, job_type="news", limit=5)
    assert [r["run_id"] for r in runs] == [news_run]


async def test_list_recent_runs_respects_limit(pg_conn) -> None:
    for i in range(3):
        run_id = await start_run(pg_conn, run_date=dt.date(2026, 8, 11 + i), job_type="news")
        await finish_run(pg_conn, run_id=run_id, status="succeeded", counters={})

    runs = await list_recent_runs(pg_conn, job_type="news", limit=2)
    assert len(runs) == 2
