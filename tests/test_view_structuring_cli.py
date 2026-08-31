"""Tests for market-view structuring pool and command-line boundaries."""

from __future__ import annotations

import sys
from typing import Any

import pytest

import agent.views as views_mod


class _ConnectionContext:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *args: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def connection(self) -> _ConnectionContext:
        return _ConnectionContext(self._conn)


@pytest.mark.asyncio
async def test_run_view_structuring_with_pool_closes_the_pool(monkeypatch) -> None:
    conn = object()
    pool = _Pool(conn)
    calls: list[object] = []

    async def fake_open_pool() -> _Pool:
        calls.append("open")
        return pool

    async def fake_run(passed_conn: object, date: str, **kwargs: Any) -> dict[str, int]:
        calls.append((passed_conn, date, kwargs))
        return {"views_structured": 2}

    async def fake_close_pool(passed_pool: object) -> None:
        calls.append(("close", passed_pool))

    monkeypatch.setattr(views_mod, "open_pool", fake_open_pool)
    monkeypatch.setattr(views_mod, "run_view_structuring", fake_run)
    monkeypatch.setattr(views_mod, "close_pool", fake_close_pool)

    result = await views_mod.run_view_structuring_with_pool(
        "2026-08-04",
        llm=lambda messages: "{}",
        extraction_model="extractor-model",
    )

    assert result == {"views_structured": 2}
    assert calls[0] == "open"
    assert calls[1][0] is conn
    assert calls[-1] == ("close", pool)


def test_main_builds_client_and_forwards_cli_options(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Client:
        model = "configured-model"

        def complete(self, messages: list[dict[str, str]], **kwargs: object) -> str:
            return "{}"

    async def fake_run(date: str, **kwargs: object) -> dict[str, int]:
        calls.append((date, kwargs))
        return {"views_structured": 3}

    monkeypatch.setattr(views_mod, "ChatClient", _Client)
    monkeypatch.setattr(views_mod, "run_view_structuring_with_pool", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent.views", "2026-08-04", "--limit", "7"],
    )

    views_mod.main()

    assert calls[0][0] == "2026-08-04"
    assert calls[0][1]["extraction_model"] == "configured-model"
    assert calls[0][1]["limit"] == 7
    complete = calls[0][1]["llm"]
    assert complete.keywords == {"json_mode": True, "max_tokens": 800}
