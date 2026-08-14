"""Agent run audit repository."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.types.json import Jsonb

VALID_STATUSES = frozenset({"succeeded", "failed", "aborted"})


async def start_run(
    conn: psycopg.AsyncConnection,
    *,
    run_date: dt.date,
    job_type: str,
) -> uuid.UUID:
    """Insert an agent_runs row immediately, status='started'.

    Written at the start rather than only at the end, so a killed run
    leaves a trace instead of vanishing without a record (audit BUG 7).

    Args:
        conn: An open connection.
        run_date: The market date this run processes.
        job_type: 'news' | 'fetch' | 'views' | 'filings' | 'profiles'.

    Returns:
        The new run_id.
    """
    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO agent_runs (run_date, job_type) VALUES (%s, %s) RETURNING run_id",
                (run_date, job_type),
            )
        ).fetchone()
    return row[0]


async def finish_run(
    conn: psycopg.AsyncConnection,
    *,
    run_id: uuid.UUID,
    status: str,
    counters: dict[str, int],
    error: str | None = None,
) -> None:
    """Mark a run finished, recording its outcome counters.

    Args:
        conn: An open connection.
        run_id: The run to finish.
        status: 'succeeded', 'failed', or 'aborted'.
        counters: Per-job outcome counts, e.g. {'articles_fetched': 10}.
            Stored as JSONB rather than fixed columns, so a new counter is
            never a schema change.
        error: Failure message, when status is 'failed' or 'aborted'.

    Raises:
        ValueError: If status is not 'succeeded', 'failed', or 'aborted'.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}")
    async with conn.transaction():
        await conn.execute(
            "UPDATE agent_runs SET status = %s, finished_at = NOW(), counters = %s, error = %s "
            "WHERE run_id = %s",
            (status, Jsonb(counters), error, run_id),
        )
