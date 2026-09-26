"""Single read-only web app for job status and MCP-backed universe discovery.

The application uses a SELECT-only role for audit rows and invokes MCP for
market data; it exposes no database writes or direct market-data queries.
"""

from __future__ import annotations

import secrets
from base64 import b64decode

import uvicorn
from loguru import logger
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from dashboard import config, logs, render
from dashboard.universe import universe_page
from data.db.pool import close_pool, open_pool
from data.db.runs import list_recent_runs

# Job types the deployment is expected to produce, so one that never ran is
# shown as missing instead of merely being absent from the history.
EXPECTED_JOBS = ["daily_refresh", "fetch", "news", "views", "filings", "profiles"]
RUN_HISTORY_LIMIT = 100


class BasicAuthMiddleware:
    """Require HTTP Basic credentials whose password matches the token.

    Basic rather than bearer because the only client is a browser: it makes
    the native credential prompt do the work instead of adding a login page
    and a session cookie to a service that holds no session state. The
    username is ignored; the password is the shared secret.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        """Wrap `app`, rejecting requests without the matching secret."""
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check credentials for http scopes; pass everything else through."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        presented = headers.get(b"authorization", b"").decode("latin-1")
        if not self._authorized(presented):
            response = PlainTextResponse(
                "unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="marketmind"'},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _authorized(self, header: str) -> bool:
        """Return True when the header carries the expected shared secret."""
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            decoded = b64decode(encoded, validate=True).decode("utf-8")
        except Exception:  # noqa: BLE001 - any malformed header is simply unauthorized
            return False
        _, _, password = decoded.partition(":")
        return secrets.compare_digest(password, self._token)


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


def build_app(token: str) -> Starlette:
    """Build the authenticated dashboard application.

    Args:
        token: Shared secret required as the Basic-auth password.

    Returns:
        A Starlette app serving only GET routes.
    """
    app = Starlette(
        routes=[
            Route("/", overview, methods=["GET"]),
            Route("/universe/", universe_page, methods=["GET"]),
            Route("/logs/{name:str}", log_file, methods=["GET"]),
        ]
    )
    app.add_middleware(BasicAuthMiddleware, token=token)
    return app


def main() -> None:
    """Run the dashboard.

    Binds loopback by default: reaching it from another machine is an SSH
    port-forward, not an open port. Set MARKETMIND_DASHBOARD_HOST only when
    a reverse proxy terminating TLS sits in front of it.
    """
    config.dsn()
    app = build_app(config.token())
    uvicorn.run(app, host=config.host(), port=config.port(), log_level="info")


if __name__ == "__main__":
    main()
