"""Read-only operations dashboard rendering and log access limits."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dashboard import config, logs, render
from dashboard.app import EXPECTED_JOBS, build_app, main


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> TestClient:
    """A dashboard client with a stubbed database and a temporary log dir."""
    monkeypatch.setenv(config.LOG_DIRS_ENV, str(tmp_path))

    async def no_runs():
        return [], None

    async def no_universe(_date: str, _query: str, _offset: int) -> dict[str, object]:
        return {"total": 0, "rows": []}

    monkeypatch.setattr("dashboard.app._recent_runs", no_runs)
    monkeypatch.setattr("dashboard.universe.fetch_universe", no_universe)
    return TestClient(build_app())


def test_dashboard_routes_are_available_without_browser_credentials(client: TestClient) -> None:
    for path in ("/", "/logs/daily.log", "/universe/"):
        response = client.get(path)
        assert response.status_code != 401


def test_the_dashboard_refuses_to_use_an_unset_database_dsn(monkeypatch) -> None:
    monkeypatch.delenv(config.DSN_ENV, raising=False)
    with pytest.raises(RuntimeError, match=config.DSN_ENV):
        config.dsn()
    monkeypatch.setenv(config.DSN_ENV, "   ")
    with pytest.raises(RuntimeError, match=config.DSN_ENV):
        config.dsn()


def test_web_app_requires_mcp_at_startup(monkeypatch) -> None:
    monkeypatch.setenv(config.DSN_ENV, "postgresql://marketmind_ro@localhost/test")
    monkeypatch.delenv("MARKETMIND_MCP_TOKEN", raising=False)
    monkeypatch.delenv("MARKETMIND_MCP_TOKEN_FILE", raising=False)
    with pytest.raises(RuntimeError, match="MARKETMIND_MCP_TOKEN"):
        main()


def test_it_binds_loopback_unless_told_otherwise(monkeypatch) -> None:
    monkeypatch.delenv(config.HOST_ENV, raising=False)
    assert config.host() == "127.0.0.1"
    monkeypatch.setenv(config.HOST_ENV, "0.0.0.0")
    assert config.host() == "0.0.0.0"


def test_overview_renders_and_flags_jobs_that_never_ran(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "never run" in response.text
    assert "href='/universe/'" in response.text
    for job in EXPECTED_JOBS:
        assert job in response.text


def test_overview_survives_a_database_outage(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(config.LOG_DIRS_ENV, str(tmp_path))

    async def failing():
        return [], "Could not read run history: connection refused"

    monkeypatch.setattr("dashboard.app._recent_runs", failing)
    response = TestClient(build_app()).get("/")

    assert response.status_code == 200
    assert "connection refused" in response.text


def test_log_names_cannot_escape_the_configured_directories(tmp_path: Path) -> None:
    (tmp_path / "daily.log").write_text("ok", encoding="utf-8")
    outside = tmp_path.parent / "secret.log"
    outside.write_text("private", encoding="utf-8")

    assert logs.resolve_log([tmp_path], "daily.log") is not None
    assert logs.resolve_log([tmp_path], "../secret.log") is None
    assert logs.resolve_log([tmp_path], "/etc/passwd") is None
    assert logs.resolve_log([tmp_path], "daily.log.bak") is None
    assert logs.resolve_log([tmp_path], "missing.log") is None


def test_a_traversing_request_is_not_served(client: TestClient, tmp_path: Path) -> None:
    (tmp_path.parent / "secret.log").write_text("private", encoding="utf-8")

    response = client.get("/logs/..%2Fsecret.log")

    assert response.status_code == 404
    assert "private" not in response.text


def test_navigation_is_available_on_both_operations_pages(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "daily.log").write_text("ok", encoding="utf-8")
    for path in ("/", "/logs/daily.log"):
        response = client.get(path)
        assert response.status_code == 200
        assert "href='/universe/'" in response.text
        assert "href='/' aria-current='page'" in response.text


def test_a_log_file_is_served_from_its_tail(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "daily.log").write_text("first line\nsecond line\n", encoding="utf-8")

    response = client.get("/logs/daily.log")

    assert response.status_code == 200
    assert "second line" in response.text


def test_tail_bounds_a_large_file_and_reports_truncation(tmp_path: Path) -> None:
    path = tmp_path / "big.log"
    path.write_text("\n".join(f"line {i}" for i in range(50_000)), encoding="utf-8")

    text, truncated = logs.tail(path, max_bytes=2048)

    assert truncated
    assert len(text.encode("utf-8")) <= 2048
    assert "line 49999" in text
    assert "line 0\n" not in text


def test_an_unreadable_log_directory_is_reported_not_fatal(tmp_path: Path) -> None:
    missing = tmp_path / "nope"

    files, problems = logs.list_logs([missing])

    assert files == []
    assert any("nope" in problem for problem in problems)


def test_run_rows_render_with_counters_and_errors() -> None:
    now = dt.datetime.now(dt.UTC)
    html = render.render_overview(
        runs=[
            {
                "job_type": "fetch",
                "status": "failed",
                "run_date": dt.date(2026, 9, 5),
                "started_at": now - dt.timedelta(minutes=5),
                "finished_at": now - dt.timedelta(minutes=4),
                "counters": {"prices": {"fetched": 2}},
                "error": "yfinance timed out",
            }
        ],
        expected_jobs=["fetch"],
        logs=[],
        problems=[],
    )

    assert "yfinance timed out" in html
    assert "status-failed" in html
    assert "&quot;fetched&quot;: 2" in html


def test_rendered_run_content_is_escaped() -> None:
    """Counters and errors come from job output, so they are never raw HTML."""
    html = render.render_overview(
        runs=[
            {
                "job_type": "news",
                "status": "failed",
                "run_date": dt.date(2026, 9, 5),
                "started_at": dt.datetime.now(dt.UTC),
                "finished_at": None,
                "counters": {},
                "error": "<script>alert(1)</script>",
            }
        ],
        expected_jobs=["news"],
        logs=[],
        problems=[],
    )

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
