"""Detached orchestration for the complete daily market-data refresh."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from loguru import logger

from data.db.pool import close_pool, open_pool
from data.db.runs import finish_run, start_run

ROOT_DIR = Path(__file__).resolve().parent.parent
REFRESH_DIR_ENV = "MARKETMIND_REFRESH_DIR"
INHERITED_LOCK_FD_ENV = "MARKETMIND_REFRESH_LOCK_FD"
REFRESH_RUN_ID_ENV = "MARKETMIND_REFRESH_RUN_ID"
REFRESH_LOG_PATH_ENV = "MARKETMIND_REFRESH_LOG_PATH"
LOCK_FILENAME = "daily-refresh.lock"
STATUS_FILENAME = "daily-refresh.json"
CommandRunner = Callable[..., subprocess.CompletedProcess[Any]]
ProcessLauncher = Callable[..., subprocess.Popen[Any]]


def _reap_process(process: subprocess.Popen[Any]) -> None:
    """Wait for a detached child so its exit status does not become a zombie."""
    process.wait()


def refresh_dir() -> Path:
    """Return the mutable directory used for refresh locks, status, and logs."""
    configured = os.getenv(REFRESH_DIR_ENV)
    return Path(configured) if configured else ROOT_DIR / "state" / "refresh"


def lock_path() -> Path:
    """Return the shared cross-process refresh lock path."""
    return refresh_dir() / LOCK_FILENAME


def status_path() -> Path:
    """Return the persisted refresh-status path."""
    return refresh_dir() / STATUS_FILENAME


def _write_status(payload: dict[str, object]) -> None:
    """Atomically persist JSON status with owner-only permissions."""
    directory = refresh_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".daily-refresh-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        os.chmod(temp_path, 0o600)
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temp_path, status_path())


def _read_status() -> dict[str, object] | None:
    """Read persisted status, returning a diagnostic for malformed state."""
    path = status_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"state": "unknown", "error": f"cannot read refresh status: {exc}"}
    if not isinstance(payload, dict):
        return {"state": "unknown", "error": "refresh status is not a JSON object"}
    return payload


def _try_lock() -> int | None:
    """Acquire the shared non-blocking lock and return its file descriptor."""
    directory = refresh_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Read-only is intentional: a host-side cron `flock` may have created
    # this shared bind-mounted file as root. Advisory locks do not require a
    # writable descriptor, and the unprivileged service can still join the
    # same lock domain.
    fd = os.open(lock_path(), os.O_RDONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def refresh_status() -> dict[str, object]:
    """Return persisted refresh metadata plus the live lock state.

    Returns:
        A dict with `active` and `run`. A stale running record is reported as
        interrupted when no process still owns the shared lock.
    """
    fd = _try_lock()
    active = fd is None
    if fd is not None:
        os.close(fd)
    run = _read_status()
    if run and run.get("state") == "running" and not active:
        run = {
            **run,
            "state": "interrupted",
            "error": run.get("error") or "refresh process exited without recording an outcome",
        }
    return {"active": active, "run": run}


def launch_refresh(
    as_of: str,
    *,
    launcher: ProcessLauncher = subprocess.Popen,
) -> dict[str, object]:
    """Reserve the refresh slot and spawn the complete daily job detached.

    Args:
        as_of: Market date in YYYY-MM-DD format.
        launcher: Process factory injected by tests.

    Returns:
        JSON-friendly acceptance or refusal details.

    Raises:
        ValueError: If `as_of` is not a valid ISO date.
        OSError: If the process cannot be started.
    """
    dt.date.fromisoformat(as_of)
    fd = _try_lock()
    if fd is None:
        current = _read_status() or {}
        return {
            "started": False,
            "run_id": current.get("run_id"),
            "log_path": current.get("log_path"),
            "reason": "another data job holds the refresh lock",
        }

    run_id = uuid.uuid4().hex
    log_dir = refresh_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"daily-refresh-{run_id}.log"
    started_at = dt.datetime.now(dt.UTC).isoformat()
    payload: dict[str, object] = {
        "run_id": run_id,
        "as_of": as_of,
        "state": "running",
        "started_at": started_at,
        "finished_at": None,
        "pid": None,
        "log_path": str(log_path),
        "error": None,
    }
    _write_status(payload)

    env = os.environ.copy()
    env[INHERITED_LOCK_FD_ENV] = str(fd)
    env[REFRESH_RUN_ID_ENV] = run_id
    env[REFRESH_LOG_PATH_ENV] = str(log_path)
    log_handle = log_path.open("a", encoding="utf-8")
    os.chmod(log_path, 0o600)
    try:
        process = launcher(
            [sys.executable, "-m", "agent.refresh", as_of],
            cwd=ROOT_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            pass_fds=(fd,),
        )
    except Exception as exc:
        payload.update(
            state="failed",
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
            error=f"{type(exc).__name__}: {exc}",
        )
        _write_status(payload)
        raise
    finally:
        log_handle.close()
        os.close(fd)

    # The child records its own PID and outcome. Do not rewrite `payload`
    # after spawn: a fast child could already have persisted its final state.
    threading.Thread(
        target=_reap_process,
        args=(process,),
        name=f"marketmind-refresh-reaper-{run_id[:8]}",
        daemon=True,
    ).start()
    return {"started": True, "run_id": run_id, "log_path": str(log_path)}


def refresh_commands(as_of: str) -> list[list[str]]:
    """Return the ordered commands composing one complete daily refresh.

    Args:
        as_of: Market date in YYYY-MM-DD format.

    Returns:
        Price/macro, news/fact, and view-structuring commands in order.
    """
    dt.date.fromisoformat(as_of)
    return [
        [sys.executable, "-m", "data.universe.resolve"],
        [sys.executable, "data/fetch.py", "--as-of", as_of],
        [sys.executable, "-m", "agent.ingestion", as_of],
        [sys.executable, "-m", "agent.views", as_of],
    ]


def run_refresh_commands(
    as_of: str,
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[str, int]:
    """Run each daily-refresh command in order, stopping on failure.

    Args:
        as_of: Market date in YYYY-MM-DD format.
        runner: Command runner injected by tests.

    Returns:
        A count of successfully completed steps.

    Raises:
        subprocess.CalledProcessError: If any step exits unsuccessfully.
    """
    completed = 0
    for command in refresh_commands(as_of):
        logger.info(f"daily refresh step starting: {' '.join(command[1:])}")
        runner(command, cwd=ROOT_DIR, check=True)
        completed += 1
    return {"steps_completed": completed}


async def _start_audit(as_of: str) -> uuid.UUID:
    """Create the umbrella PostgreSQL audit row for a refresh."""
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            return await start_run(
                conn,
                run_date=dt.date.fromisoformat(as_of),
                job_type="daily_refresh",
            )
    finally:
        await close_pool(pool)


async def _finish_audit(
    audit_run_id: uuid.UUID,
    *,
    status: str,
    counters: dict[str, int],
    error: str | None = None,
) -> None:
    """Finish the umbrella PostgreSQL audit row for a refresh."""
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            await finish_run(
                conn,
                run_id=audit_run_id,
                status=status,
                counters=counters,
                error=error,
            )
    finally:
        await close_pool(pool)


def _initial_payload(as_of: str, run_id: str) -> dict[str, object]:
    """Build status metadata for a directly started refresh process."""
    return {
        "run_id": run_id,
        "as_of": as_of,
        "state": "running",
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "finished_at": None,
        "pid": os.getpid(),
        "log_path": os.getenv(REFRESH_LOG_PATH_ENV),
        "error": None,
    }


def execute_refresh(as_of: str, *, lock_fd: int, run_id: str) -> None:
    """Execute and audit a refresh while holding an already-acquired lock.

    Args:
        as_of: Market date in YYYY-MM-DD format.
        lock_fd: Descriptor owning the shared refresh lock.
        run_id: External run identifier stored in refresh metadata.

    Raises:
        Exception: After recording a failed outcome for any job or audit error.
    """
    payload = _read_status() or _initial_payload(as_of, run_id)
    payload.update(run_id=run_id, as_of=as_of, state="running", pid=os.getpid())
    _write_status(payload)
    counters = {"steps_completed": 0}
    audit_run_id: uuid.UUID | None = None
    try:
        audit_run_id = asyncio.run(_start_audit(as_of))
        payload["audit_run_id"] = str(audit_run_id)
        _write_status(payload)
        counters = run_refresh_commands(as_of)
        asyncio.run(_finish_audit(audit_run_id, status="succeeded", counters=counters))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if audit_run_id is not None:
            try:
                asyncio.run(
                    _finish_audit(
                        audit_run_id,
                        status="failed",
                        counters=counters,
                        error=error,
                    )
                )
            except Exception:
                logger.exception("failed to finish daily-refresh audit row")
        payload.update(
            state="failed",
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
            error=error,
        )
        _write_status(payload)
        raise
    else:
        payload.update(
            state="succeeded",
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
            error=None,
            counters=counters,
        )
        _write_status(payload)
    finally:
        os.close(lock_fd)


def _lock_for_process() -> int | None:
    """Return the inherited lock descriptor or acquire the lock directly."""
    inherited = os.getenv(INHERITED_LOCK_FD_ENV)
    if inherited is not None:
        return int(inherited)
    return _try_lock()


def main(argv: Sequence[str] | None = None) -> None:
    """Run the complete daily refresh as a lock-protected job entry point."""
    parser = argparse.ArgumentParser(description="Complete daily market-data refresh")
    parser.add_argument("as_of", nargs="?", default=dt.date.today().isoformat())
    args = parser.parse_args(argv)
    try:
        dt.date.fromisoformat(args.as_of)
    except ValueError:
        parser.error("as_of must be YYYY-MM-DD")

    fd = _lock_for_process()
    if fd is None:
        logger.warning("daily refresh skipped: another data job holds the lock")
        return
    run_id = os.getenv(REFRESH_RUN_ID_ENV) or uuid.uuid4().hex
    execute_refresh(args.as_of, lock_fd=fd, run_id=run_id)


if __name__ == "__main__":
    main()
