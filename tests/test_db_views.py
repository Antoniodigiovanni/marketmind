"""Tests for the market-views repository -- writes, as-of reads, structuring."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest

from data.db.companies import create_company
from data.db.views import get_market_views, save_market_view, structure_views

pytestmark = pytest.mark.asyncio


async def _company(pg_conn, name: str = "TestCo") -> uuid.UUID:
    return await create_company(
        pg_conn, name=name, country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )


async def test_save_market_view_returns_a_uuid(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    assert isinstance(view_id, uuid.UUID)


async def test_get_market_views_returns_what_was_saved(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 5))
    assert len(views) == 1
    assert views[0]["view_id"] == view_id
    assert views[0]["view_text"] == "Rates path is the key swing factor for H2."
    assert views[0]["model"] == "hermes"
    assert views[0]["company_id"] is None
    assert views[0]["used_fact_ids"] is None
    assert views[0]["structure"] is None
    assert views[0]["structure_schema_version"] is None
    assert views[0]["structured_by"] is None
    assert views[0]["structured_at"] is None


async def test_get_market_views_is_point_in_time(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="future-looking",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    before = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 3))
    assert before == []


async def test_get_market_views_orders_newest_first(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 7, 1),
        scope="market",
        view_text="old",
        model="hermes",
        data_cutoff=dt.date(2026, 7, 1),
    )
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="new",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    assert len(await get_market_views(pg_conn, as_of=dt.date(2026, 7, 15))) == 1
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 5))
    assert [v["view_text"] for v in views] == ["new", "old"]


async def test_save_market_view_with_used_fact_ids_round_trips(pg_conn) -> None:
    fact_ids = [uuid.uuid4(), uuid.uuid4()]
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
        used_fact_ids=fact_ids,
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 5))
    assert views[0]["view_id"] == view_id
    assert set(views[0]["used_fact_ids"]) == set(fact_ids)


async def test_save_market_view_ticker_scope_round_trips_company_id(pg_conn) -> None:
    company_id = await _company(pg_conn)
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="ticker",
        company_id=company_id,
        view_text="AAPL-specific view.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4), company_id=company_id)
    assert [v["view_id"] for v in views] == [view_id]


async def test_save_market_view_ticker_scope_without_company_id_violates_db_constraint(
    pg_conn,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        await save_market_view(
            pg_conn,
            as_of=dt.date(2026, 8, 4),
            scope="ticker",
            view_text="orphaned",
            model="hermes",
            data_cutoff=dt.date(2026, 8, 4),
        )


async def test_get_market_views_filters_by_scope(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="theme",
        theme="semiconductors",
        view_text="Semis cycle turning up.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Broad view.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4), scope="theme")
    assert len(views) == 1
    assert views[0]["theme"] == "semiconductors"


async def test_get_market_views_filters_by_company_id(pg_conn) -> None:
    company_a = await _company(pg_conn, "CompanyA")
    company_b = await _company(pg_conn, "CompanyB")
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="ticker",
        company_id=company_a,
        view_text="About A.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="ticker",
        company_id=company_b,
        view_text="About B.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4), company_id=company_a)
    assert [v["view_text"] for v in views] == ["About A."]


async def test_get_market_views_respects_limit(pg_conn) -> None:
    for i in range(3):
        await save_market_view(
            pg_conn,
            as_of=dt.date(2026, 8, 4),
            scope="market",
            view_text=f"view {i}",
            model="hermes",
            data_cutoff=dt.date(2026, 8, 4),
        )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4), limit=2)
    assert len(views) == 2


@pytest.mark.parametrize("schema_version", [0, True])
async def test_get_market_views_rejects_invalid_schema_version(
    pg_conn, schema_version: int
) -> None:
    with pytest.raises(ValueError, match="schema_version"):
        await get_market_views(
            pg_conn,
            as_of=dt.date(2026, 8, 4),
            schema_version=schema_version,
        )


async def test_structure_views_with_fake_extractor(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Bullish semis.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )

    async def fake_extractor(view: dict[str, object]) -> dict[str, object]:
        assert view["view_id"] == view_id
        assert view["as_of"] == dt.date(2026, 8, 4)
        assert view["scope"] == "market"
        return {"stance": "bullish", "thesis": view["view_text"]}

    structured = await structure_views(
        pg_conn, extractor=fake_extractor, extraction_model="test-model"
    )
    assert structured == 1

    row = await (
        await pg_conn.execute(
            "SELECT structure, extracted_by FROM market_view_structures WHERE view_id = %s",
            (view_id,),
        )
    ).fetchone()
    assert row[0]["stance"] == "bullish"
    assert row[1] == "test-model"


async def test_structure_views_is_idempotent_per_schema_version(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Neutral.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )

    async def v1_extractor(view: dict[str, object]) -> dict[str, object]:
        return {"stance": "neutral", "version": 1}

    async def v2_extractor(view: dict[str, object]) -> dict[str, object]:
        return {"stance": "bullish", "version": 2}

    assert (
        await structure_views(
            pg_conn,
            extractor=v1_extractor,
            extraction_model="test-model",
            schema_version=1,
        )
        == 1
    )
    assert (
        await structure_views(
            pg_conn,
            extractor=v1_extractor,
            extraction_model="test-model",
            schema_version=1,
        )
        == 0
    )
    # A different schema_version is not blocked by the v1 row (todo.md A1.9).
    assert (
        await structure_views(
            pg_conn,
            extractor=v2_extractor,
            extraction_model="test-model",
            schema_version=2,
        )
        == 1
    )

    latest = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4))
    assert latest[0]["structure"] == {"stance": "bullish", "version": 2}
    assert latest[0]["structure_schema_version"] == 2
    assert latest[0]["structured_by"] == "test-model"
    assert latest[0]["structured_at"] is not None

    version_one = await get_market_views(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        schema_version=1,
    )
    assert version_one[0]["structure"] == {"stance": "neutral", "version": 1}
    assert version_one[0]["structure_schema_version"] == 1


async def test_structure_views_respects_as_of_and_limit(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="eligible one",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="eligible two",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 6),
        scope="market",
        view_text="future",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 6),
    )

    seen: list[str] = []

    async def fake_extractor(view: dict[str, object]) -> dict[str, object]:
        seen.append(str(view["view_text"]))
        return {"stance": "neutral"}

    assert (
        await structure_views(
            pg_conn,
            extractor=fake_extractor,
            extraction_model="test-model",
            as_of=dt.date(2026, 8, 5),
            limit=1,
        )
        == 1
    )
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("extraction_model", "schema_version", "limit", "message"),
    [
        (" ", 1, 1, "extraction_model"),
        ("test-model", 0, 1, "schema_version"),
        ("test-model", True, 1, "schema_version"),
        ("test-model", 1, 0, "limit"),
        ("test-model", 1, True, "limit"),
    ],
)
async def test_structure_views_rejects_nonpositive_bounds(
    pg_conn, extraction_model: str, schema_version: int, limit: int, message: str
) -> None:
    async def fake_extractor(view: dict[str, object]) -> dict[str, object]:
        return {}

    with pytest.raises(ValueError, match=message):
        await structure_views(
            pg_conn,
            extractor=fake_extractor,
            extraction_model=extraction_model,
            schema_version=schema_version,
            limit=limit,
        )
