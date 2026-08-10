"""Tests for universe sync and point-in-time membership reads."""

from __future__ import annotations

import datetime as dt

import pytest

from data.db.universe import get_universe_members, sync_universe

pytestmark = pytest.mark.asyncio

SP500_ROWS = [
    "ticker,name,country,sector,added_date,removed_date",
    "AAPL,Apple Inc.,US,Technology,1990-01-01,",
]
EUROSTOXX_ROWS = [
    "ticker,name,country,sector,added_date,removed_date",
    "ASML,ASML Holding,NL,Semiconductors,2000-01-01,",
    "OLDCO,Old Company,FR,Industrials,1995-01-01,2010-06-30",
]


def _write_universe(tmp_path) -> None:
    (tmp_path / "sp500.csv").write_text("\n".join(SP500_ROWS) + "\n", encoding="utf-8")
    (tmp_path / "eurostoxx600.csv").write_text("\n".join(EUROSTOXX_ROWS) + "\n", encoding="utf-8")


def _stub_search(id_value: str, *, name_hint: str | None = None) -> dict[str, str] | None:
    known = {"AAPL": "AAPL", "ASML": "ASML.AS"}
    symbol = known.get(id_value)
    return {"symbol": symbol, "name": id_value} if symbol else None


def _stub_check(symbol: str) -> bool:
    return symbol != "ASML.AS"  # force ASML into 'review' to exercise that branch


async def test_sync_universe_reports_stats(pg_conn, tmp_path) -> None:
    _write_universe(tmp_path)
    stats = await sync_universe(
        pg_conn, universe_dir=tmp_path, searcher=_stub_search, checker=_stub_check
    )
    # 3 members total (AAPL, ASML, OLDCO); OLDCO is already removed so it is
    # never resolved and does not count toward valid/review/unresolved.
    assert stats["members"] == 3
    assert stats["valid"] == 1  # AAPL
    assert stats["review"] == 1  # ASML
    assert stats["unresolved"] == 0


async def test_sync_universe_is_idempotent(pg_conn, tmp_path) -> None:
    _write_universe(tmp_path)
    await sync_universe(pg_conn, universe_dir=tmp_path, searcher=_stub_search, checker=_stub_check)
    stats = await sync_universe(
        pg_conn, universe_dir=tmp_path, searcher=_stub_search, checker=_stub_check
    )
    assert stats["members"] == 3


async def test_sync_universe_retries_unresolved_members_once(pg_conn, tmp_path) -> None:
    _write_universe(tmp_path)
    attempts: dict[str, int] = {}

    def flaky_search(id_value: str, *, name_hint: str | None = None) -> dict[str, str] | None:
        attempts[id_value] = attempts.get(id_value, 0) + 1
        if id_value == "AAPL" and attempts[id_value] == 1:
            return None  # fails first pass, succeeds on retry
        return _stub_search(id_value, name_hint=name_hint)

    stats = await sync_universe(
        pg_conn, universe_dir=tmp_path, searcher=flaky_search, checker=_stub_check
    )
    assert stats["unresolved"] == 0
    assert stats["valid"] == 1
    assert attempts["AAPL"] == 2


async def test_get_universe_members_is_point_in_time(pg_conn, tmp_path) -> None:
    _write_universe(tmp_path)
    await sync_universe(pg_conn, universe_dir=tmp_path, searcher=_stub_search, checker=_stub_check)

    current = await get_universe_members(
        pg_conn, universe="eurostoxx600", as_of=dt.date(2026, 1, 1)
    )
    names = {row["name"] for row in current}
    assert names == {"ASML Holding"}  # OLDCO left in 2010

    historical = await get_universe_members(
        pg_conn, universe="eurostoxx600", as_of=dt.date(2005, 1, 1)
    )
    names = {row["name"] for row in historical}
    assert names == {"ASML Holding", "Old Company"}


async def test_get_universe_members_includes_yahoo_symbol(pg_conn, tmp_path) -> None:
    _write_universe(tmp_path)
    await sync_universe(pg_conn, universe_dir=tmp_path, searcher=_stub_search, checker=_stub_check)

    members = await get_universe_members(pg_conn, universe="sp500", as_of=dt.date(2026, 1, 1))
    assert members == [
        {
            "company_id": members[0]["company_id"],
            "name": "Apple Inc.",
            "country": "US",
            "sector": "Technology",
            "added_date": dt.date(1990, 1, 1),
            "removed_date": None,
            "yahoo_symbol": "AAPL",
        }
    ]


async def test_sync_universe_resync_with_corrected_symbol_does_not_duplicate_listing(
    pg_conn, tmp_path
) -> None:
    """A member unresolved on one sync (fallback listing = raw ticker) that
    resolves to a real, different symbol on a later sync must end up with
    exactly one live listing, not two -- upsert_listing is keyed on the
    resolved symbol, so a naive port of resolve.py's retry (which is keyed on
    the stable ticker) would otherwise leave the fallback row live forever
    and fan out get_universe_members."""
    _write_universe(tmp_path)

    def never_resolves(id_value: str, *, name_hint: str | None = None) -> dict[str, str] | None:
        return None

    await sync_universe(
        pg_conn, universe_dir=tmp_path, searcher=never_resolves, checker=_stub_check
    )
    before = await get_universe_members(pg_conn, universe="eurostoxx600", as_of=dt.date(2026, 1, 1))
    asml_before = next(row for row in before if row["name"] == "ASML Holding")
    assert asml_before["yahoo_symbol"] == "ASML"  # ticker fallback

    await sync_universe(pg_conn, universe_dir=tmp_path, searcher=_stub_search, checker=_stub_check)
    after = await get_universe_members(pg_conn, universe="eurostoxx600", as_of=dt.date(2026, 1, 1))
    asml_rows = [row for row in after if row["name"] == "ASML Holding"]
    assert len(asml_rows) == 1
    assert asml_rows[0]["yahoo_symbol"] == "ASML.AS"


async def test_get_universe_members_yahoo_symbol_is_none_when_delisted(pg_conn, tmp_path) -> None:
    _write_universe(tmp_path)
    await sync_universe(pg_conn, universe_dir=tmp_path, searcher=_stub_search, checker=_stub_check)

    historical = await get_universe_members(
        pg_conn, universe="eurostoxx600", as_of=dt.date(2005, 1, 1)
    )
    oldco = next(row for row in historical if row["name"] == "Old Company")
    assert oldco["yahoo_symbol"] is None
