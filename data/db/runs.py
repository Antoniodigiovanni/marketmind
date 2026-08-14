"""Agent run audit repository."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.rows import dict_row
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


async def list_recent_runs(
    conn: psycopg.AsyncConnection,
    *,
    job_type: str | None = None,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Return the most recent agent_runs rows, newest first.

    Args:
        conn: An open connection.
        job_type: Optional filter, e.g. 'news' or 'filings'; all job types
            when omitted.
        limit: Maximum rows.

    Returns:
        One dict per run: run_id, run_date, job_type, status, started_at,
        finished_at, counters (a nested dict, not flattened -- that is the
        whole point of counters being JSONB instead of fixed columns),
        error.
    """
    query = (
        "SELECT run_id, run_date, job_type, status, started_at, finished_at, counters, error "
        "FROM agent_runs WHERE 1 = 1"
    )
    params: list[object] = []
    if job_type is not None:
        query += " AND job_type = %s"
        params.append(job_type)
    query += " ORDER BY started_at DESC LIMIT %s"
    params.append(limit)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()
