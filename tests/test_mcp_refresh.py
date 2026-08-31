"""Offline tests for the complete-refresh MCP tools."""

from __future__ import annotations

import datetime as dt
import threading
import uuid

import pytest

import server.jobs as jobs
import server.mcp as mcp_mod

DATE = "2026-08-04"


@pytest.fixture(autouse=True)
def _clear_slot(monkeypatch) -> None:
    """Give every test an empty in-process run slot."""
    monkeypatch.setattr(jobs, "_CURRENT", None)


def test_run_daily_refresh_starts_the_detached_job(monkeypatch) -> None:
    expected = {"started": True, "run_id": "refresh-1", "log_path": "/tmp/refresh.log"}
    seen: list[str] = []

    def fake_launch(as_of: str) -> dict[str, object]:
        seen.append(as_of)
        return expected

    monkeypatch.setattr(mcp_mod, "launch_refresh", fake_launch)

    assert mcp_mod.run_daily_refresh(DATE) == expected
    assert seen == [DATE]


def test_run_daily_refresh_rejects_invalid_date_without_spawning(monkeypatch) -> None:
    called = False

    def fake_launch(as_of: str) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(mcp_mod, "launch_refresh", fake_launch)

    result = mcp_mod.run_daily_refresh("04-08-2026")

    assert result["started"] is False
    assert "Invalid date" in result["reason"]
    assert called is False


def test_run_daily_refresh_refuses_an_in_process_job() -> None:
    release = threading.Event()
    first_id = jobs.start_run("news", lambda progress: release.wait(timeout=5) and {})
    try:
        result = mcp_mod.run_daily_refresh(DATE)
    finally:
        release.set()
        record = jobs.current_run()
        assert record is not None
        record.thread.join(timeout=5)

    assert result["started"] is False
    assert result["run_id"] == first_id
    assert "already in flight" in result["reason"]


def test_run_daily_refresh_reports_process_start_failure(monkeypatch) -> None:
    def fail(as_of: str) -> dict[str, object]:
        raise OSError("process limit reached")

    monkeypatch.setattr(mcp_mod, "launch_refresh", fail)

    result = mcp_mod.run_daily_refresh(DATE)

    assert result["started"] is False
    assert result["run_id"] is None
    assert result["reason"] == "Cannot start daily refresh: process limit reached"


async def test_get_refresh_status_combines_lock_state_and_history(monkeypatch) -> None:
    class _FakePool:
        def connection(self) -> _FakePool:
            return self

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def fake_pool() -> _FakePool:
        return _FakePool()

    async def fake_recent_runs(conn: object, *, limit: int) -> list[dict[str, object]]:
        assert limit == 1
        return [
            {
                "run_id": uuid.uuid4(),
                "run_date": dt.date.fromisoformat(DATE),
                "job_type": "daily_refresh",
                "status": "succeeded",
                "started_at": dt.datetime(2026, 8, 4, tzinfo=dt.UTC),
                "finished_at": dt.datetime(2026, 8, 4, 1, tzinfo=dt.UTC),
                "counters": {"steps_completed": 3},
                "error": None,
            }
        ]

    monkeypatch.setattr(mcp_mod, "_pg_pool", fake_pool)
    monkeypatch.setattr(mcp_mod, "list_recent_runs", fake_recent_runs)
    monkeypatch.setattr(
        mcp_mod,
        "refresh_status",
        lambda: {"active": False, "run": {"run_id": "refresh-1", "state": "succeeded"}},
    )

    result = await mcp_mod.get_refresh_status(limit=1)

    assert result["refresh"]["run"]["run_id"] == "refresh-1"
    assert result["history"][0]["job_type"] == "daily_refresh"
    assert result["history"][0]["run_date"] == DATE
    with pytest.raises(ValueError, match="between 1 and 100"):
        await mcp_mod.get_refresh_status(limit=0)
