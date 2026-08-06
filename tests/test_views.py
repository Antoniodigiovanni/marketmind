"""Tests for market views and the structured extraction job."""

from __future__ import annotations

import pytest

from agent.views import get_market_views, save_market_view, structure_views
from data.db.init import init_db


@pytest.fixture()
def conn():
    connection = init_db(":memory:")
    yield connection
    connection.close()


def test_save_and_get_round_trip(conn) -> None:
    view_id = save_market_view(
        conn,
        as_of="2026-08-04",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        scope="market",
    )
    views = get_market_views(conn, as_of="2026-08-05")
    assert len(views) == 1
    assert views[0]["id"] == view_id
    assert views[0]["view_text"] == "Rates path is the key swing factor for H2."


def test_save_and_get_used_fact_ids_round_trip(conn) -> None:
    view_id = save_market_view(
        conn,
        as_of="2026-08-04",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        scope="market",
        used_fact_ids=["fact-1", "fact-2"],
    )
    views = get_market_views(conn, as_of="2026-08-05")
    assert views[0]["id"] == view_id
    assert views[0]["used_fact_ids"] == ["fact-1", "fact-2"]


def test_save_without_used_fact_ids_defaults_none(conn) -> None:
    save_market_view(
        conn,
        as_of="2026-08-04",
        view_text="No provenance.",
        model="hermes",
        scope="market",
    )
    views = get_market_views(conn, as_of="2026-08-05")
    assert views[0]["used_fact_ids"] is None


def test_get_market_views_filters_by_as_of(conn) -> None:
    save_market_view(conn, as_of="2026-07-01", view_text="old", model="hermes")
    save_market_view(conn, as_of="2026-08-04", view_text="new", model="hermes")

    assert len(get_market_views(conn, as_of="2026-07-15")) == 1
    assert [v["view_text"] for v in get_market_views(conn, as_of="2026-08-05")] == ["new", "old"]


def test_ticker_scope_validation(conn) -> None:
    with pytest.raises(ValueError, match="ticker is required"):
        save_market_view(conn, as_of="2026-08-04", view_text="x", model="hermes", scope="ticker")
    with pytest.raises(ValueError, match="only allowed"):
        save_market_view(conn, as_of="2026-08-04", view_text="x", model="hermes", ticker="AAPL")


def test_theme_scope_round_trip(conn) -> None:
    view_id = save_market_view(
        conn,
        as_of="2026-08-04",
        view_text="Semis cycle turning up.",
        model="hermes",
        scope="theme",
        theme="semiconductors",
    )
    views = get_market_views(conn, as_of="2026-08-04", scope="theme")
    assert views[0]["id"] == view_id


def test_structure_views_with_fake_extractor(conn) -> None:
    save_market_view(
        conn, as_of="2026-08-04", view_text="Bullish semis.", model="hermes", scope="market"
    )

    def fake_extractor(view_id: str, view_text: str, model: str) -> dict[str, object]:
        return {
            "stance": "bullish",
            "thesis": view_text,
            "risks": [],
            "catalysts": [],
            "confidence": 0.7,
        }

    structured = structure_views(conn, extractor=fake_extractor)
    assert structured == 1

    rows = conn.execute("SELECT view_id, payload FROM market_view_structures").fetchall()
    assert len(rows) == 1
    assert "bullish" in rows[0][1]


def test_structure_views_is_idempotent(conn) -> None:
    save_market_view(conn, as_of="2026-08-04", view_text="Neutral.", model="hermes", scope="market")

    def fake_extractor(view_id: str, view_text: str, model: str) -> dict[str, object]:
        return {"stance": "neutral"}

    assert structure_views(conn, extractor=fake_extractor) == 1
    assert structure_views(conn, extractor=fake_extractor) == 0


def test_default_extractor_raises_not_implemented(conn) -> None:
    save_market_view(conn, as_of="2026-08-04", view_text="x", model="hermes", scope="market")
    with pytest.raises(NotImplementedError):
        structure_views(conn)
