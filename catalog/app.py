"""Browser-facing, read-only universe list; market data is fetched through MCP."""

from __future__ import annotations

import datetime as dt
import html
import os
import secrets
from base64 import b64decode
from urllib.parse import urlencode

import httpx
import uvicorn
from loguru import logger
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from dashboard.render import STYLE, render_navigation
from data.credentials import credential

PAGE_SIZE = 50


class BasicAuth:
    """Authenticate every browser request with a separate browser secret."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        """Wrap the application with Basic authentication."""
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject unauthorized HTTP requests before accessing market data."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, encoded = header.partition(" ")
        try:
            decoded = b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            decoded = ""
        _, separator, password = decoded.partition(":")
        if (
            scheme.lower() != "basic"
            or not separator
            or not secrets.compare_digest(password, self.token)
        ):
            response = PlainTextResponse(
                "unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="marketmind catalog"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def fetch_universe(date: str, query: str, offset: int) -> dict[str, object]:
    """Call the bounded MCP universe tool with a server-side credential."""
    token = credential("MARKETMIND_MCP_TOKEN")
    if not token:
        raise RuntimeError("MARKETMIND_MCP_TOKEN must be set")
    url = os.getenv("MARKETMIND_CATALOG_MCP_URL", "http://127.0.0.1:8000/mcp")
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client,
        streamablehttp_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "list_universe",
            {"as_of": date, "query": query, "offset": offset, "limit": PAGE_SIZE},
        )
        if result.isError or not isinstance(result.structuredContent, dict):
            raise RuntimeError("MCP universe lookup failed")
        return result.structuredContent


def render_universe(
    date: str, query: str, offset: int, data: dict[str, object], *, operations_url: str
) -> str:
    """Render a searchable, paginated, escaped stock table with shared navigation."""
    rows = data["rows"]
    total = int(data["total"])
    assert isinstance(rows, list)
    body_rows = (
        "".join(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(row.get(key) or '—'))}</td>"
                for key in ("ticker", "name", "universe", "country", "sector", "added_date")
            )
            + "</tr>"
            for row in rows
        )
        or "<tr><td colspan='6'>No stocks match this date and search.</td></tr>"
    )

    def link(new_offset: int, label: str) -> str:
        params = urlencode({"as_of": date, "q": query, "offset": new_offset})
        return f"<a href='?{html.escape(params, quote=True)}'>{label}</a>"

    links = []
    if offset:
        links.append(link(max(0, offset - PAGE_SIZE), "← Previous"))
    if offset + PAGE_SIZE < total:
        links.append(link(offset + PAGE_SIZE, "Next →"))
    navigation = " · ".join(links)
    section_nav = render_navigation(
        active="universe", operations_url=operations_url, universe_url="./"
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Marketmind — universe</title>"
        f"<style>{STYLE}</style></head><body><main>{section_nav}"
        "<h1>Marketmind — universe</h1>"
        "<p class='sub'>Read-only constituents as of the selected date.</p>"
        "<form method='get' action='./'>"
        "<label for='as_of'>As of</label> "
        f"<input id='as_of' name='as_of' type='date' required value='{html.escape(date, quote=True)}'> "
        "<label for='q'>Ticker or name</label> "
        f"<input id='q' name='q' type='search' maxlength='100' value='{html.escape(query, quote=True)}'> "
        "<button type='submit'>Search</button></form>"
        f"<p role='status'>{total:,} stocks · showing {offset + 1 if rows else 0}"
        f"–{offset + len(rows) if rows else 0}</p>"
        "<div class='scroll'><table><thead><tr><th>Ticker</th><th>Name</th><th>Universe</th>"
        "<th>Country</th><th>Sector</th><th>Added</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div><nav aria-label='Pages'>{navigation}</nav>"
        "</main></body></html>"
    )


async def universe_page(request: Request) -> Response:
    """Validate page controls and display the matching MCP universe page."""
    date = request.query_params.get("as_of", dt.date.today().isoformat())
    query = request.query_params.get("q", "").strip()
    offset_text = request.query_params.get("offset", "0")
    try:
        if dt.date.fromisoformat(date).isoformat() != date or len(query) > 100:
            raise ValueError("invalid date or search")
        offset = int(offset_text)
        if offset < 0 or offset > 1_000_000:
            raise ValueError("invalid offset")
    except ValueError:
        return PlainTextResponse("Invalid date, search, or offset", status_code=400)
    try:
        data = await fetch_universe(date, query, offset)
    except Exception:  # noqa: BLE001 - no backend credentials or internals in the browser
        logger.exception("catalog could not fetch universe from MCP")
        return PlainTextResponse("Universe is temporarily unavailable", status_code=503)
    return HTMLResponse(
        render_universe(
            date,
            query,
            offset,
            data,
            operations_url=os.getenv("MARKETMIND_DASHBOARD_PUBLIC_URL", "http://127.0.0.1:8001/"),
        )
    )


def build_app(token: str) -> Starlette:
    """Build an authenticated read-only browser with one GET route."""
    app = Starlette(routes=[Route("/", universe_page, methods=["GET"])])
    app.add_middleware(BasicAuth, token=token)
    return app


def main() -> None:
    """Serve the catalog, requiring distinct browser and MCP credentials."""
    token = credential("MARKETMIND_CATALOG_TOKEN")
    if not token or not credential("MARKETMIND_MCP_TOKEN"):
        raise RuntimeError("MARKETMIND_CATALOG_TOKEN and MARKETMIND_MCP_TOKEN are required")
    uvicorn.run(
        build_app(token),
        host=os.getenv("MARKETMIND_CATALOG_HOST", "127.0.0.1"),
        port=int(os.getenv("MARKETMIND_CATALOG_PORT", "8002")),
    )


if __name__ == "__main__":
    main()
