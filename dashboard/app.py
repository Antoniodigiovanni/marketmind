"""Single read-only web app for job status and MCP-backed universe discovery.

The application uses a SELECT-only role for audit rows and invokes MCP for
market data; it exposes no database writes or direct market-data queries.
"""

from __future__ import annotations

import uvicorn
from loguru import logger
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from dashboard import config, logs, render
from dashboard.universe import universe_page
from data.credentials import credential
from data.db.pool import close_pool, open_pool
from data.db.runs import list_recent_runs

# Job types the deployment is expected to produce, so one that never ran is
# shown as missing instead of merely being absent from the history.
EXPECTED_JOBS = ["daily_refresh", "fetch", "news", "views", "filings", "profiles"]
RUN_HISTORY_LIMIT = 100


async def _recent_runs() -> tuple[list[dict[str, object]], str | None]:
    """Return recent audit rows, or an empty list and a message on failure."""
    pool = await open_pool(config.dsn())
    try:
        async with pool.connection() as conn:
            return await list_recent_runs(conn, limit=RUN_HISTORY_LIMIT), None
    except Exception as exc:  # noqa: BLE001 - a database outage must still render the page
        logger.exception("dashboard could not read agent_runs")
        return [], f"Could not read run history: {exc}"
    finally:
        await close_pool(pool)


async def overview(request: Request) -> Response:
    """Render the run overview and the list of readable log files."""
    runs, db_error = await _recent_runs()
    files, problems = logs.list_logs(config.log_dirs())
    return HTMLResponse(
        render.render_overview(
            runs=runs,
            expected_jobs=EXPECTED_JOBS,
            logs=files,
            problems=problems,
            db_error=db_error,
        )
    )


async def log_file(request: Request) -> Response:
    """Render the tail of one allowed log file."""
    name = request.path_params["name"]
    path = logs.resolve_log(config.log_dirs(), name)
    if path is None:
        return PlainTextResponse("not found", status_code=404)
    try:
        text, truncated = logs.tail(path)
    except (OSError, PermissionError) as exc:
        return PlainTextResponse(f"cannot read {name}: {exc.strerror or exc}", status_code=403)
    return HTMLResponse(render.render_log(name=name, text=text, truncated=truncated))


def build_app() -> Starlette:
    """Build the dashboard application.

    Returns:
        A Starlette app serving only GET routes. Network access is constrained
        by loopback binding and optional Tailscale ACLs, not app-level browser
        credentials.
    """
    app = Starlette(
        routes=[
            Route("/", overview, methods=["GET"]),
            Route("/universe/", universe_page, methods=["GET"]),
            Route("/logs/{name:str}", log_file, methods=["GET"]),
        ]
    )
    return app


def main() -> None:
    """Run the dashboard.

    Binds loopback by default: reaching it from another machine requires a
    constrained private path such as Tailscale Serve with ACLs or SSH. Set
    MARKETMIND_DASHBOARD_HOST only when that private access layer sits in
    front of it.
    """
    config.dsn()
    if not credential("MARKETMIND_MCP_TOKEN"):
        raise RuntimeError("MARKETMIND_MCP_TOKEN is required for the universe page")
    app = build_app()
    uvicorn.run(app, host=config.host(), port=config.port(), log_level="info")


if __name__ == "__main__":
    main()
