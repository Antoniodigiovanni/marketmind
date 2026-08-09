"""Single-slot background runner for the long-running MCP job tools."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

# (tickers_done, tickers_total, running summary counts)
ProgressCallback = Callable[[int, int, dict[str, int]], None]
JobWork = Callable[[ProgressCallback], dict[str, int]]


class RunInProgress(RuntimeError):
    """Raised when a run is requested while another one is still running."""


@dataclass
class RunRecord:
    """Bookkeeping for one background job run."""

    run_id: str
    job_type: str
    started_at: dt.datetime
    state: str = "running"
    finished_at: dt.datetime | None = None
    tickers_done: int = 0
    tickers_total: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-friendly view of the run for the status tool.

        While the run is still going, `finished_at` is None and
        `elapsed_seconds` is measured up to now.
        """
        end = self.finished_at or dt.datetime.now(dt.UTC)
        return {
            "run_id": self.run_id,
            "job_type": self.job_type,
            "state": self.state,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_seconds": round((end - self.started_at).total_seconds(), 3),
            "tickers_done": self.tickers_done,
            "tickers_total": self.tickers_total,
            "counts": dict(self.counts),
            "error": self.error,
        }


_CURRENT: RunRecord | None = None
_LOCK = threading.Lock()


def current_run() -> RunRecord | None:
    """Return the most recent run record, running or finished."""
    return _CURRENT


def _execute_run(record: RunRecord, work: JobWork) -> None:
    """Run `work` synchronously, recording the outcome on `record`."""

    def progress(done: int, total: int, counts: dict[str, int]) -> None:
        record.tickers_done = done
        record.tickers_total = total
        record.counts = dict(counts)

    try:
        record.counts = work(progress)
        record.state = "succeeded"
    except Exception as exc:  # noqa: BLE001 - the outcome is polled, never raised
        record.state = "failed"
        record.error = str(exc)
        logger.exception(f"{record.job_type} run {record.run_id} failed")
    finally:
        record.finished_at = dt.datetime.now(dt.UTC)


def start_run(job_type: str, work: JobWork) -> str:
    """Claim the single run slot and execute `work` on a background thread.

    Args:
        job_type: 'news' or 'filings'; recorded on the run.
        work: Callable receiving a progress callback and returning counts.

    Returns:
        The new run id.

    Raises:
        RunInProgress: If another run is still running.
    """
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None and _CURRENT.state == "running":
            raise RunInProgress(
                f"Run {_CURRENT.run_id} ({_CURRENT.job_type}) already in flight "
                f"since {_CURRENT.started_at.isoformat()}"
            )
        record = RunRecord(
            run_id=uuid.uuid4().hex,
            job_type=job_type,
            started_at=dt.datetime.now(dt.UTC),
        )
        record.thread = threading.Thread(
            target=_execute_run,
            args=(record, work),
            name=f"marketmind-{job_type}-{record.run_id[:8]}",
            daemon=True,
        )
        _CURRENT = record
    record.thread.start()
    return record.run_id
