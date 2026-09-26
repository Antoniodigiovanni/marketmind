"""Browser authentication, pagination, escaping and MCP universe contract."""

from __future__ import annotations

from base64 import b64encode

import polars as pl
import pytest
from starlette.testclient import TestClient

from catalog.app import build_app
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

    monkeypatch.setattr("catalog.app.fetch_universe", fake_fetch)
    return TestClient(build_app("secret"))


def test_browser_is_authenticated_and_escapes_data(client: TestClient) -> None:
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert client.post("/", headers=_auth()).status_code == 405
    response = client.get("/?as_of=2026-01-01", headers=_auth())
    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "<script>" not in response.text
    assert "A &amp; B" in response.text
    assert "offset=50" in response.text
    assert "href='http://127.0.0.1:8001/'" in response.text
    assert "aria-current='page'" in response.text


def test_paging_and_invalid_inputs(client: TestClient) -> None:
    response = client.get("/?as_of=2026-01-01&offset=50", headers=_auth())
    assert response.status_code == 200
    assert "Previous" in response.text
    assert "No stocks match" in response.text
    assert "href='?as_of=2026-01-01" in response.text
    assert "action='./'" in response.text
    for query in ("as_of=2026-02-30", "offset=-1", "q=" + "x" * 101):
        assert client.get("/?" + query, headers=_auth()).status_code == 400


def test_catalog_link_uses_configured_dashboard_url(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MARKETMIND_DASHBOARD_PUBLIC_URL", "/")
    response = client.get("/?as_of=2026-01-01", headers=_auth())
    assert "href='/'" in response.text
    assert "href='./' aria-current='page'" in response.text


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
    import asyncio

    page = asyncio.run(mcp_mod.list_universe("2026-01-01", offset=1, limit=1))
    assert page["total"] == 3
    assert [row["ticker"] for row in page["rows"]] == ["OLD"]
    assert asyncio.run(mcp_mod.list_universe("2026-01-01", query="a & b"))["total"] == 1
    for kwargs in ({"limit": 101}, {"offset": -1}, {"as_of": "2026-02-30"}):
        with pytest.raises(ValueError):
            asyncio.run(mcp_mod.list_universe(**kwargs))
