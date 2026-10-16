"""First regressions for shared MCP job reservations and filing archives."""

from __future__ import annotations

import asyncio
import selectors
import subprocess
import sys
from pathlib import Path

import polars as pl
import pytest

import agent.ingestion as ingestion
import agent.refresh as refresh
import data.filings.fetch as filings
import server.jobs as jobs
import server.mcp as mcp_mod

DATE = "2026-08-04"


@pytest.fixture(autouse=True)
def _isolated_jobs(isolated_jobs, monkeypatch: pytest.MonkeyPatch):
    """Stub providers after the common job isolation fixture is active."""
    universe = pl.DataFrame({"ticker": ["AAPL"]})
    monkeypatch.setattr(mcp_mod, "get_universe", lambda date: universe)
    monkeypatch.setattr(filings, "get_universe", lambda date: universe)
    monkeypatch.setattr(mcp_mod, "_build_edgar_client", object)
    monkeypatch.setattr(filings, "EdgardClient", object)
    monkeypatch.setattr(mcp_mod, "_build_llm", lambda: object())
    monkeypatch.setattr(ingestion, "_default_profile_builder", lambda llm: object())


@pytest.mark.parametrize("job_type", ["news", "filings"])
def test_mcp_thread_job_refuses_external_shared_lock_before_work(
    job_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled job or backup must prevent acceptance and audit registration."""
    work_entered: list[str] = []

    async def unexpected_work(*args: object, **kwargs: object) -> dict[str, int]:
        # Both real pool wrappers register their audit only after entering here.
        work_entered.append(job_type)
        return {}

    monkeypatch.setattr(ingestion, "run_daily_ingest_with_pool", unexpected_work)
    monkeypatch.setattr(filings, "run_daily_fetch_with_pool", unexpected_work)
    with subprocess.Popen(
        ["flock", "-n", str(refresh.lock_path()), "sh", "-c", "echo locked; read -r release"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as holder:
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(holder.stdout, selectors.EVENT_READ)
                assert ready.select(timeout=5), "external flock holder did not become ready"
            assert holder.stdout.readline().strip() == "locked"
            assert holder.poll() is None

            if job_type == "news":
                result = mcp_mod.run_ingest(DATE)
            else:
                result = mcp_mod.run_filings_refresh(DATE)

            record = jobs.current_run()
            if record is not None and record.thread is not None and record.thread.ident is not None:
                record.thread.join(timeout=5)
            assert "refusing" in result.lower(), result
            assert jobs.current_run() is None, "busy refusal must not allocate a run record"
            assert work_entered == [], "busy refusal must precede work and audit registration"
        finally:
            holder.communicate(input="release\n", timeout=5)
    assert holder.returncode == 0


def test_filings_cli_and_mcp_use_the_same_raw_archive_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI and MCP must pass the same data/raw archive root to the fetcher."""
    roots: list[Path] = []

    async def capture_fetch(*args: object, root: Path, **kwargs: object) -> dict[str, int]:
        roots.append(root)
        return {}

    monkeypatch.setattr(filings, "run_daily_fetch_with_pool", capture_fetch)
    monkeypatch.setattr(sys, "argv", ["filings.fetch", "--run-date", DATE])
    filings.main()

    result = mcp_mod.run_filings_refresh(DATE)
    assert "started" in result.lower(), result
    record = jobs.current_run()
    assert record is not None and record.thread is not None
    record.thread.join(timeout=5)
    assert not record.thread.is_alive()
    assert record.state == "succeeded", record.error

    assert len(roots) == 2
    assert roots[0] == Path(filings.__file__).resolve().parents[1] / "raw"
    assert roots[1] == roots[0], "MCP filing archives must share the CLI data/raw default"


def _probe_lock() -> int:
    """Try the shared lock from an independent process."""
    return subprocess.run(["flock", "-n", str(refresh.lock_path()), "true"], check=False).returncode


def test_worker_cancelled_error_releases_reservation_and_allows_next_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must finish the failed run and allow the next job to succeed."""
    import threading

    entered = threading.Event()
    release = threading.Event()
    uncaught: list[BaseException] = []
    # Keep an uncaught cancellation observable without a pytest thread-warning failure.
    monkeypatch.setattr(threading, "excepthook", lambda args: uncaught.append(args.exc_value))

    def work(progress: jobs.ProgressCallback) -> dict[str, int]:
        entered.set()
        assert release.wait(5)
        raise asyncio.CancelledError("worker cancelled")

    jobs.start_run("news", work)
    record = jobs.current_run()
    try:
        assert entered.wait(5), "worker did not start"
    finally:
        release.set()
        record.thread.join()

    assert not record.thread.is_alive()
    assert all(isinstance(exc, asyncio.CancelledError) for exc in uncaught)
    assert _probe_lock() == 0, "cancelled worker must release the independent flock"
    assert record.finished_at is not None
    assert record.state == "failed"

    run_id = jobs.start_run("filings", lambda progress: {"done": 1})
    next_record = jobs.current_run()
    try:
        assert next_record is not record
        assert next_record.run_id == run_id
    finally:
        next_record.thread.join()
    assert not next_record.thread.is_alive()
    assert next_record.state == "succeeded", next_record.error
    assert next_record.finished_at is not None
    assert next_record.counts == {"done": 1}


@pytest.mark.parametrize("fail", [False, True])
def test_reservation_lasts_until_worker_exit(fail: bool) -> None:
    """Real flock contenders remain excluded until success or failure cleanup."""
    import threading

    entered = threading.Event()
    release = threading.Event()

    def work(progress: jobs.ProgressCallback) -> dict[str, int]:
        entered.set()
        assert release.wait(5)
        if fail:
            raise RuntimeError("work failed")
        return {"done": 1}

    jobs.start_run("news", work)
    record = jobs.current_run()
    try:
        assert entered.wait(5)
        assert _probe_lock() == 1
        assert refresh.launch_refresh(DATE)["started"] is False
    finally:
        release.set()
        record.thread.join(5)
    assert record.state == ("failed" if fail else "succeeded")
    assert record.finished_at is not None
    assert _probe_lock() == 0


@pytest.mark.parametrize("stage", ["record", "constructor", "start"])
def test_setup_failures_release_reservation(stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every failure after acquisition permits a later job and leaves no running state."""
    probes: list[int] = []

    def fail(*args: object, **kwargs: object) -> None:
        probes.append(_probe_lock())
        raise RuntimeError("setup failed")

    with monkeypatch.context() as patch:
        if stage == "record":
            patch.setattr(jobs, "RunRecord", fail)
        elif stage == "constructor":
            patch.setattr(jobs.threading, "Thread", fail)
        else:
            patch.setattr(jobs.threading.Thread, "start", fail)
        with pytest.raises(RuntimeError, match="setup failed"):
            jobs.start_run("news", lambda progress: {})
    assert probes == [1], "setup must happen under the shared reservation"
    record = jobs.current_run()
    assert record is None or record.state == "failed"
    assert _probe_lock() == 0
    jobs.start_run("filings", lambda progress: {})
    jobs.current_run().thread.join(5)
    assert jobs.current_run().state == "succeeded"


def test_concurrent_thread_starters_accept_exactly_one() -> None:
    """Competing requests cannot both reserve the job slot."""
    import concurrent.futures
    import threading

    barrier = threading.Barrier(8)
    release = threading.Event()

    def work(progress: jobs.ProgressCallback) -> dict[str, int]:
        assert release.wait(5)
        return {}

    def start() -> bool:
        barrier.wait(5)
        try:
            jobs.start_run("news", work)
        except jobs.RunInProgress:
            return False
        return True

    try:
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: start(), range(8)))
        assert sum(results) == 1
        assert _probe_lock() == 1
    finally:
        release.set()
        jobs.current_run().thread.join(5)


def test_independent_job_processes_compete_for_reservation() -> None:
    """A second MCP process cannot accept work while the first owns the lock."""
    script = """
import sys, threading
from server import jobs
release = threading.Event()
try:
    jobs.start_run("news", lambda progress: (release.wait(10), {})[1])
except jobs.RunInProgress:
    print("busy", flush=True)
else:
    print("accepted", flush=True)
    sys.stdin.readline()
    release.set()
    jobs.current_run().thread.join(5)
"""
    with subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as first:
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(first.stdout, selectors.EVENT_READ)
                assert ready.select(5)
            assert first.stdout.readline().strip() == "accepted"
            second = subprocess.run(
                [sys.executable, "-c", script],
                input="release\n",
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            assert second.stdout.strip() == "busy"
        finally:
            first.communicate("release\n", timeout=10)
    assert first.returncode == 0
    assert _probe_lock() == 0


def test_refresh_child_uses_inherited_reservation_without_real_jobs() -> None:
    """The nested refresh entry point accepts its inherited descriptor without relocking."""
    import os

    fd = refresh._try_lock()
    assert fd is not None
    script = """
from agent import refresh
async def start(*args, **kwargs):
    return "audit"
async def finish(*args, **kwargs):
    pass
refresh._start_audit = start
refresh._finish_audit = finish
refresh.run_refresh_commands = lambda date: {"steps_completed": 5}
refresh.main()
"""
    env = os.environ.copy()
    env[refresh.INHERITED_LOCK_FD_ENV] = str(fd)
    try:
        subprocess.run(
            [sys.executable, "-c", script, DATE],
            env=env,
            pass_fds=(fd,),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert _probe_lock() == 1
        assert refresh.refresh_status()["run"]["state"] == "succeeded"
    finally:
        os.close(fd)
    assert _probe_lock() == 0
