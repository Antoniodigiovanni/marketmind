"""Tests for detached daily-refresh orchestration."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.refresh as refresh

DATE = "2026-08-31"


@pytest.fixture(autouse=True)
def _refresh_dir(tmp_path: Path, monkeypatch) -> Path:
    """Keep refresh state and logs inside each test's temporary directory."""
    monkeypatch.setenv(refresh.REFRESH_DIR_ENV, str(tmp_path))
    return tmp_path


def test_refresh_commands_are_ordered_and_date_bounded() -> None:
    commands = refresh.refresh_commands(DATE)

    assert commands[0][1:] == ["-m", "data.universe.resolve"]
    assert commands[1][1:] == ["data/fetch.py", "--as-of", DATE]
    assert commands[2][1:] == ["-m", "agent.ingestion", DATE]
    assert commands[3][1:] == ["-m", "agent.views", DATE]
    with pytest.raises(ValueError):
        refresh.refresh_commands("31-08-2026")


def test_run_refresh_commands_stops_after_first_failure() -> None:
    seen: list[list[str]] = []

    def runner(command, **kwargs):
        seen.append(command)
        if len(seen) == 2:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(subprocess.CalledProcessError):
        refresh.run_refresh_commands(DATE, runner=runner)

    assert len(seen) == 2


def test_launch_refresh_passes_the_lock_to_a_detached_child(_refresh_dir: Path) -> None:
    captured: dict[str, object] = {}

    def launcher(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(pid=4321, wait=lambda: 0)

    result = refresh.launch_refresh(DATE, launcher=launcher)

    assert result["started"] is True
    assert result["run_id"]
    assert Path(str(result["log_path"])).parent == _refresh_dir / "logs"
    assert captured["command"][1:3] == ["-m", "agent.refresh"]
    assert captured["start_new_session"] is True
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["stderr"] == subprocess.STDOUT
    assert len(captured["pass_fds"]) == 1
    assert captured["env"][refresh.REFRESH_RUN_ID_ENV] == result["run_id"]

    persisted = json.loads(refresh.status_path().read_text(encoding="utf-8"))
    assert persisted["state"] == "running"
    assert persisted["pid"] is None
    assert refresh.status_path().stat().st_mode & 0o777 == 0o600


def test_launch_refresh_refuses_when_shared_lock_is_held() -> None:
    refresh.refresh_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(refresh.lock_path(), os.O_RDONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = refresh.launch_refresh(DATE)
    finally:
        os.close(fd)

    assert result == {
        "started": False,
        "run_id": None,
        "log_path": None,
        "reason": "another data job holds the refresh lock",
    }


def test_refresh_status_marks_an_unlocked_running_record_interrupted() -> None:
    refresh._write_status(
        {
            "run_id": "dead-run",
            "state": "running",
            "error": None,
        }
    )

    status = refresh.refresh_status()

    assert status["active"] is False
    assert status["run"]["state"] == "interrupted"
    assert "without recording an outcome" in status["run"]["error"]


def test_execute_refresh_records_success_and_releases_lock(monkeypatch) -> None:
    refresh.refresh_dir().mkdir(parents=True, exist_ok=True)
    fd = refresh._try_lock()
    audit_id = uuid.uuid4()
    finished: list[dict[str, object]] = []

    async def fake_start(as_of: str):
        assert as_of == DATE
        return audit_id

    async def fake_finish(run_id, **kwargs):
        assert run_id == audit_id
        finished.append(kwargs)

    monkeypatch.setattr(refresh, "_start_audit", fake_start)
    monkeypatch.setattr(refresh, "_finish_audit", fake_finish)
    monkeypatch.setattr(refresh, "run_refresh_commands", lambda as_of: {"steps_completed": 3})

    refresh.execute_refresh(DATE, lock_fd=fd, run_id="run-1")

    status = refresh.refresh_status()
    assert status["active"] is False
    assert status["run"]["state"] == "succeeded"
    assert status["run"]["counters"] == {"steps_completed": 3}
    assert status["run"]["audit_run_id"] == str(audit_id)
    assert finished == [{"status": "succeeded", "counters": {"steps_completed": 3}}]


def test_execute_refresh_records_failure_and_releases_lock(monkeypatch) -> None:
    refresh.refresh_dir().mkdir(parents=True, exist_ok=True)
    fd = refresh._try_lock()
    audit_id = uuid.uuid4()
    finished: list[dict[str, object]] = []

    async def fake_start(as_of: str):
        return audit_id

    async def fake_finish(run_id, **kwargs):
        finished.append(kwargs)

    def fail(as_of: str):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(refresh, "_start_audit", fake_start)
    monkeypatch.setattr(refresh, "_finish_audit", fake_finish)
    monkeypatch.setattr(refresh, "run_refresh_commands", fail)

    with pytest.raises(RuntimeError, match="source unavailable"):
        refresh.execute_refresh(DATE, lock_fd=fd, run_id="run-2")

    status = refresh.refresh_status()
    assert status["active"] is False
    assert status["run"]["state"] == "failed"
    assert status["run"]["error"] == "RuntimeError: source unavailable"
    assert finished == [
        {
            "status": "failed",
            "counters": {"steps_completed": 0},
            "error": "RuntimeError: source unavailable",
        }
    ]
