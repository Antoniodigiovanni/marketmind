"""Tests for the single-slot background job runner."""

from __future__ import annotations

import os
import threading

import pytest

import agent.refresh as refresh
import server.jobs as jobs

pytestmark = pytest.mark.usefixtures("isolated_jobs")


def test_job_lock_environment_is_isolated(tmp_path) -> None:
    """Job tests must never use a host lock directory or inherited descriptor."""
    assert refresh.refresh_dir() == tmp_path / "refresh"
    assert refresh.INHERITED_LOCK_FD_ENV not in os.environ


def test_start_run_executes_work_and_records_counts() -> None:
    def work(progress: jobs.ProgressCallback) -> dict[str, int]:
        return {"facts_written": 3}

    run_id = jobs.start_run("news", work)
    record = jobs.current_run()
    assert record is not None
    record.thread.join(timeout=5)

    assert record.run_id == run_id
    assert record.job_type == "news"
    assert record.state == "succeeded"
    assert record.counts == {"facts_written": 3}
    assert record.error is None
    assert record.finished_at is not None


def test_second_run_is_refused_while_one_is_in_flight() -> None:
    release = threading.Event()

    def blocking_work(progress: jobs.ProgressCallback) -> dict[str, int]:
        release.wait(timeout=5)
        return {}

    first_id = jobs.start_run("news", blocking_work)
    try:
        with pytest.raises(jobs.RunInProgress, match=first_id):
            jobs.start_run("filings", lambda progress: {})
    finally:
        release.set()
        jobs.current_run().thread.join(timeout=5)


def test_a_finished_run_frees_the_slot() -> None:
    jobs.start_run("news", lambda progress: {})
    jobs.current_run().thread.join(timeout=5)

    second_id = jobs.start_run("filings", lambda progress: {})
    jobs.current_run().thread.join(timeout=5)

    assert jobs.current_run().run_id == second_id


def test_a_raising_job_is_recorded_as_failed_not_propagated() -> None:
    def exploding_work(progress: jobs.ProgressCallback) -> dict[str, int]:
        raise RuntimeError("12 consecutive extraction failures")

    jobs.start_run("news", exploding_work)
    record = jobs.current_run()
    record.thread.join(timeout=5)

    assert record.state == "failed"
    assert record.error == "12 consecutive extraction failures"
    assert record.finished_at is not None


def test_progress_callback_updates_the_record() -> None:
    def reporting_work(progress: jobs.ProgressCallback) -> dict[str, int]:
        progress(1, 2, {"facts_written": 1})
        progress(2, 2, {"facts_written": 4})
        return {"facts_written": 4}

    jobs.start_run("news", reporting_work)
    record = jobs.current_run()
    record.thread.join(timeout=5)

    assert record.tickers_done == 2
    assert record.tickers_total == 2
    assert record.counts == {"facts_written": 4}


def test_progress_counts_survive_a_failure() -> None:
    def failing_midway(progress: jobs.ProgressCallback) -> dict[str, int]:
        progress(1, 3, {"facts_written": 2})
        raise RuntimeError("boom")

    jobs.start_run("news", failing_midway)
    record = jobs.current_run()
    record.thread.join(timeout=5)

    assert record.state == "failed"
    assert record.tickers_done == 1
    assert record.counts == {"facts_written": 2}


def test_snapshot_of_a_finished_run_is_json_friendly() -> None:
    jobs.start_run("news", lambda progress: {"facts_written": 1})
    record = jobs.current_run()
    record.thread.join(timeout=5)

    snapshot = record.snapshot()

    assert snapshot["run_id"] == record.run_id
    assert snapshot["job_type"] == "news"
    assert snapshot["state"] == "succeeded"
    assert snapshot["counts"] == {"facts_written": 1}
    assert snapshot["error"] is None
    assert isinstance(snapshot["started_at"], str)
    assert isinstance(snapshot["finished_at"], str)
    assert snapshot["elapsed_seconds"] >= 0.0
    assert "thread" not in snapshot


def test_snapshot_of_a_running_run_has_no_finish_time() -> None:
    release = threading.Event()
    jobs.start_run("news", lambda progress: release.wait(timeout=5) and {})
    record = jobs.current_run()
    try:
        snapshot = record.snapshot()
        assert snapshot["state"] == "running"
        assert snapshot["finished_at"] is None
        assert snapshot["elapsed_seconds"] >= 0.0
    finally:
        release.set()
        record.thread.join(timeout=5)
