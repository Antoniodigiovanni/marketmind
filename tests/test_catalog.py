"""Unified web app authentication, universe pagination and MCP contract."""

from __future__ import annotations

import asyncio
import socket
from base64 import b64encode

import polars as pl
import pytest
import uvicorn
from starlette.testclient import TestClient

from dashboard.app import build_app
from dashboard.universe import fetch_universe, render_universe
from server import mcp as mcp_mod


def _auth() -> dict[str, str]:
    return {"Authorization": "Basic " + b64encode(b"reader:secret").decode()}


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """Browser with an in-memory MCP result, never contacting the network."""

    async def fake_fetch(date: str, query: str, offset: int) -> dict[str, object]:
        assert date == "2026-01-01"
        assert query == ""
        return {
            "total": 51,
            "rows": [
                {
                    "ticker": "<script>",
                    "name": "A & B",
                    "universe": "sp500",
                    "country": "US",
                    "sector": "Tech",
                    "added_date": "2020-01-01",
                }
            ]
            if offset == 0
            else [],
        }

    monkeypatch.setattr("dashboard.universe.fetch_universe", fake_fetch)
    return TestClient(build_app("secret"))


def test_browser_is_authenticated_and_escapes_data(client: TestClient) -> None:
    assert client.get("/universe/").status_code == 401
    assert client.get("/universe/", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert client.post("/universe/", headers=_auth()).status_code == 405
    response = client.get("/universe/?as_of=2026-01-01", headers=_auth())
    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "<script>" not in response.text
    assert "A &amp; B" in response.text
    assert "offset=50" in response.text
    assert "href='/'" in response.text
    assert "href='/universe/' aria-current='page'" in response.text
    assert "aria-current='page'" in response.text


def test_paging_and_invalid_inputs(client: TestClient) -> None:
    response = client.get("/universe/?as_of=2026-01-01&offset=50", headers=_auth())
    assert response.status_code == 200
    assert "Previous" in response.text
    assert "No stocks match" in response.text
    assert "href='?as_of=2026-01-01" in response.text
    assert "action='/universe/'" in response.text
    assert "51 stocks" in response.text
    for query in ("as_of=2026-02-30", "offset=-1", "q=" + "x" * 101):
        assert client.get("/universe/?" + query, headers=_auth()).status_code == 400


def test_universe_route_redirects_to_trailing_slash(client: TestClient) -> None:
    response = client.get("/universe?as_of=2026-01-01", headers=_auth(), follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("/universe/?as_of=2026-01-01")


def test_universe_tool_searches_and_bounds_pages(monkeypatch) -> None:
    frame = pl.DataFrame(
        {
            "ticker": ["XYZ", "ABC", "OLD"],
            "name": ["Xylophone", "A & B", "Old"],
            "country": ["US"] * 3,
            "sector": ["Tech"] * 3,
            "universe": ["sp500"] * 3,
            "added_date": [None] * 3,
            "removed_date": [None] * 3,
        }
    )
    monkeypatch.setattr(mcp_mod, "get_universe", lambda date: frame)
    page = asyncio.run(mcp_mod.list_universe("2026-01-01", offset=1, limit=1))
    assert page["total"] == 3
    assert [row["ticker"] for row in page["rows"]] == ["OLD"]
    assert asyncio.run(mcp_mod.list_universe("2026-01-01", query="a & b"))["total"] == 1
    for kwargs in ({"limit": 101}, {"offset": -1}, {"as_of": "2026-02-30"}):
        with pytest.raises(ValueError):
            asyncio.run(mcp_mod.list_universe(**kwargs))


async def test_universe_page_client_uses_authenticated_mcp_http(monkeypatch) -> None:
    """Exercise the real MCP client/server contract without a database or external calls."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = mcp_mod.mcp.streamable_http_app()
    app.add_middleware(mcp_mod._BearerAuthMiddleware, token="local-test-token")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        monkeypatch.setenv("MARKETMIND_MCP_TOKEN", "local-test-token")
        monkeypatch.setenv("MARKETMIND_WEB_MCP_URL", f"http://127.0.0.1:{port}/mcp")
        result = await fetch_universe("2026-09-26", "AAPL", 0)
        assert result["total"] >= 1
        assert any(row["ticker"] == "AAPL" for row in result["rows"])
        assert "1 stock · showing 1–1" in render_universe("2026-09-26", "AAPL", 0, result)
    finally:
        server.should_exit = True
        await task
