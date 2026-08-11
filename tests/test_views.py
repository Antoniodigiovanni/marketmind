"""Tests for the market-views orchestration layer — ticker resolution + validation."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from agent.views import get_market_views, save_market_view
from data.db.identifiers import get_or_create_identifier

pytestmark = pytest.mark.asyncio


async def _ticker(pg_conn, ticker: str = "AAPL") -> uuid.UUID:
    return await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value=ticker,
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name=f"{ticker} Inc",
        country="US",
        sector="Technology",
    )


async def test_save_and_get_round_trip(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="market",
    )
    views = await get_market_views(pg_conn, as_of="2026-08-05")
    assert len(views) == 1
    assert str(views[0]["view_id"]) == view_id
    assert views[0]["view_text"] == "Rates path is the key swing factor for H2."


async def test_save_and_get_used_fact_ids_round_trip(pg_conn) -> None:
    fact_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    view_id = await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="market",
        used_fact_ids=fact_ids,
    )
    views = await get_market_views(pg_conn, as_of="2026-08-05")
    assert str(views[0]["view_id"]) == view_id
    assert sorted(str(f) for f in views[0]["used_fact_ids"]) == sorted(fact_ids)


async def test_save_without_used_fact_ids_defaults_none(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="No provenance.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="market",
    )
    views = await get_market_views(pg_conn, as_of="2026-08-05")
    assert views[0]["used_fact_ids"] is None


async def test_get_market_views_filters_by_as_of(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of="2026-07-01",
        view_text="old",
        model="hermes",
        data_cutoff="2026-07-01",
    )
    await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="new",
        model="hermes",
        data_cutoff="2026-08-04",
    )
    assert len(await get_market_views(pg_conn, as_of="2026-07-15")) == 1
    views = await get_market_views(pg_conn, as_of="2026-08-05")
    assert [v["view_text"] for v in views] == ["new", "old"]


async def test_ticker_scope_validation(pg_conn) -> None:
    with pytest.raises(ValueError, match="ticker is required"):
        await save_market_view(
            pg_conn,
            as_of="2026-08-04",
            view_text="x",
            model="hermes",
            data_cutoff="2026-08-04",
            scope="ticker",
        )
    with pytest.raises(ValueError, match="only allowed"):
        await save_market_view(
            pg_conn,
            as_of="2026-08-04",
            view_text="x",
            model="hermes",
            data_cutoff="2026-08-04",
            ticker="AAPL",
        )


async def test_ticker_scope_resolves_to_company_id(pg_conn) -> None:
    company_id = await _ticker(pg_conn, "AAPL")
    view_id = await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="AAPL-specific view.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="ticker",
        ticker="AAPL",
    )
    views = await get_market_views(pg_conn, as_of="2026-08-04", ticker="AAPL")
    assert [str(v["view_id"]) for v in views] == [view_id]
    assert views[0]["company_id"] == company_id


async def test_ticker_scope_unresolvable_ticker_raises_on_write(pg_conn) -> None:
    with pytest.raises(ValueError, match="no company found for ticker 'NOPE'"):
        await save_market_view(
            pg_conn,
            as_of="2026-08-04",
            view_text="x",
            model="hermes",
            data_cutoff="2026-08-04",
            scope="ticker",
            ticker="NOPE",
        )


async def test_get_market_views_unresolvable_ticker_returns_empty(pg_conn) -> None:
    assert await get_market_views(pg_conn, as_of="2026-08-04", ticker="NOPE") == []


async def test_theme_scope_round_trip(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="Semis cycle turning up.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="theme",
        theme="semiconductors",
    )
    views = await get_market_views(pg_conn, as_of="2026-08-04", scope="theme")
    assert str(views[0]["view_id"]) == view_id


async def test_data_cutoff_is_required(pg_conn) -> None:
    with pytest.raises(TypeError):
        await save_market_view(  # type: ignore[call-arg]
            pg_conn,
            as_of="2026-08-04",
            view_text="x",
            model="hermes",
            scope="market",
        )
