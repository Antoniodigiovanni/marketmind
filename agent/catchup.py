"""Run missing daily refreshes after a host outage."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

import psycopg

from data.db.pool import dsn_from_env

MAX_CATCHUP_DAYS = 7


def missing_dates(last_success: dt.date | None, today: dt.date) -> list[dt.date]:
    """List outstanding dates in order, refusing an unbounded backlog.

    Args:
        last_success: Most recent successful daily-refresh market date.
        today: Current date on the scheduling host.

    Returns:
        Dates to process, including today when it has not succeeded.

    Raises:
        RuntimeError: If the backlog exceeds the configured safety bound.
    """
    if last_success is None:
        return [today]
    days = (today - last_success).days
    if days > MAX_CATCHUP_DAYS:
        raise RuntimeError(f"{days} missing daily dates exceed the {MAX_CATCHUP_DAYS}-day bound")
    return [last_success + dt.timedelta(days=offset) for offset in range(1, days + 1)]


def main() -> None:
    """Query the audit log and process each missing date sequentially."""
    dsn = dsn_from_env()
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT max(run_date) FROM agent_runs "
            "WHERE job_type = 'daily_refresh' AND status = 'succeeded'"
        ).fetchone()
    last_success = row[0] if row else None
    for run_date in missing_dates(last_success, dt.date.today()):
        subprocess.run(
            [sys.executable, "-m", "agent.refresh", run_date.isoformat()],
            check=True,
            env=os.environ.copy(),
        )
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT 1 FROM agent_runs WHERE job_type = 'daily_refresh' "
                "AND run_date = %s AND status = 'succeeded' LIMIT 1",
                (run_date,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"daily refresh did not succeed for {run_date}")


if __name__ == "__main__":
    main()
