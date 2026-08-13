"""Tests for the prices repository — upsert, incremental cursor, FX conversion."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from data.db.companies import create_company
from data.db.listings import upsert_listing
from data.db.prices import (
    get_last_fx_date,
    get_last_price_date,
    get_prices,
    get_volatility,
    replace_all_volatility,
    upsert_fx_rates,
    upsert_prices,
)

pytestmark = pytest.mark.asyncio


async def _listing(pg_conn, symbol: str = "AAPL") -> uuid.UUID:
    company_id = await create_company(
        pg_conn, name="Apple", country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )
    return await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol=symbol,
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )


async def test_upsert_prices_then_get_prices_round_trip(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    written = await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 3),
                "open": 99.0,
                "high": 100.0,
                "low": 98.0,
                "close": 99.5,
                "volume": 900,
                "currency": "USD",
            },
            {
                "date": dt.date(2026, 8, 4),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1000,
                "currency": "USD",
            },
        ],
    )
    assert written == 2

    rows = await get_prices(pg_conn, listing_id=listing_id)
    assert [r["close"] for r in rows] == [99.5, 101.0]
    assert rows[0]["date"] == dt.date(2026, 8, 3)


async def test_upsert_prices_on_conflict_updates(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 4),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1000,
                "currency": "USD",
            }
        ],
    )
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 4),
                "open": 100.0,
                "high": 103.0,
                "low": 99.0,
                "close": 102.5,
                "volume": 1200,
                "currency": "USD",
            }
        ],
    )
    rows = await get_prices(pg_conn, listing_id=listing_id)
    assert len(rows) == 1
    assert rows[0]["close"] == 102.5
    assert rows[0]["volume"] == 1200


async def test_get_prices_filters_by_start_and_end(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": d,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": float(i),
                "volume": 1,
                "currency": "USD",
            }
            for i, d in enumerate([dt.date(2026, 8, 1), dt.date(2026, 8, 2), dt.date(2026, 8, 3)])
        ],
    )
    rows = await get_prices(
        pg_conn, listing_id=listing_id, start=dt.date(2026, 8, 2), end=dt.date(2026, 8, 2)
    )
    assert len(rows) == 1
    assert rows[0]["date"] == dt.date(2026, 8, 2)


async def test_get_last_price_date_returns_none_when_empty(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    assert await get_last_price_date(pg_conn, listing_id=listing_id) is None


async def test_get_last_price_date_returns_max_date(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 3),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
                "currency": "USD",
            },
            {
                "date": dt.date(2026, 8, 4),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
                "currency": "USD",
            },
        ],
    )
    assert await get_last_price_date(pg_conn, listing_id=listing_id) == dt.date(2026, 8, 4)


async def test_upsert_prices_empty_rows_is_a_noop(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    assert await upsert_prices(pg_conn, listing_id=listing_id, rows=[]) == 0


async def test_upsert_fx_rates_then_convert_get_prices(pg_conn) -> None:
    listing_id = await _listing(pg_conn, symbol="ASML.AS")
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 4),
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1,
                "currency": "EUR",
            }
        ],
    )
    await upsert_fx_rates(
        pg_conn,
        base_currency="EUR",
        rows=[
            {"quote_currency": "USD", "date": dt.date(2026, 8, 1), "rate": 1.1, "source": "ecb"},
        ],
    )
    rows = await get_prices(pg_conn, listing_id=listing_id, to_currency="USD")
    assert rows[0]["close"] == pytest.approx(110.0)


async def test_get_prices_conversion_uses_most_recent_rate_on_or_before(pg_conn) -> None:
    listing_id = await _listing(pg_conn, symbol="ASML.AS")
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 4),
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1,
                "currency": "EUR",
            }
        ],
    )
    # No rate exactly on 2026-08-04 (an FX holiday) -- only 2026-08-03 and
    # 2026-08-05. An equi-join would drop the bar; the LATERAL join must
    # pick the most recent rate on or before the bar's date (08-03, 1.2),
    # not the later one.
    await upsert_fx_rates(
        pg_conn,
        base_currency="EUR",
        rows=[
            {"quote_currency": "USD", "date": dt.date(2026, 8, 3), "rate": 1.2, "source": "ecb"},
            {"quote_currency": "USD", "date": dt.date(2026, 8, 5), "rate": 1.5, "source": "ecb"},
        ],
    )
    rows = await get_prices(pg_conn, listing_id=listing_id, to_currency="USD")
    assert rows[0]["close"] == pytest.approx(120.0)


async def test_get_prices_same_currency_is_a_noop_conversion(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 4),
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1,
                "currency": "USD",
            }
        ],
    )
    rows = await get_prices(pg_conn, listing_id=listing_id, to_currency="USD")
    assert rows[0]["close"] == pytest.approx(100.0)


async def test_upsert_fx_rates_on_conflict_updates(pg_conn) -> None:
    await upsert_fx_rates(
        pg_conn,
        base_currency="EUR",
        rows=[{"quote_currency": "USD", "date": dt.date(2026, 8, 4), "rate": 1.1, "source": "ecb"}],
    )
    await upsert_fx_rates(
        pg_conn,
        base_currency="EUR",
        rows=[
            {"quote_currency": "USD", "date": dt.date(2026, 8, 4), "rate": 1.15, "source": "ecb"}
        ],
    )
    assert await get_last_fx_date(pg_conn, base_currency="EUR", quote_currency="USD") == dt.date(
        2026, 8, 4
    )


async def test_get_last_fx_date_returns_none_when_empty(pg_conn) -> None:
    assert await get_last_fx_date(pg_conn, base_currency="EUR", quote_currency="GBP") is None


async def test_replace_all_volatility_then_get_volatility(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    written = await replace_all_volatility(
        pg_conn,
        rows=[
            {"listing_id": listing_id, "date": dt.date(2026, 8, 3), "rv_20d": 18.5, "rv_60d": 21.0},
            {"listing_id": listing_id, "date": dt.date(2026, 8, 4), "rv_20d": 19.0, "rv_60d": 21.2},
        ],
    )
    assert written == 2
    rows = await get_volatility(pg_conn, listing_id=listing_id)
    assert [r["rv_20d"] for r in rows] == [18.5, 19.0]


async def test_replace_all_volatility_truncates_previous_run(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    await replace_all_volatility(
        pg_conn,
        rows=[
            {"listing_id": listing_id, "date": dt.date(2026, 8, 3), "rv_20d": 18.5, "rv_60d": 21.0}
        ],
    )
    await replace_all_volatility(
        pg_conn,
        rows=[
            {"listing_id": listing_id, "date": dt.date(2026, 8, 4), "rv_20d": 19.0, "rv_60d": 21.2}
        ],
    )
    rows = await get_volatility(pg_conn, listing_id=listing_id)
    assert len(rows) == 1
    assert rows[0]["date"] == dt.date(2026, 8, 4)


async def test_get_volatility_filters_by_date_range(pg_conn) -> None:
    listing_id = await _listing(pg_conn)
    await replace_all_volatility(
        pg_conn,
        rows=[
            {"listing_id": listing_id, "date": dt.date(2026, 8, 3), "rv_20d": 1.0, "rv_60d": 1.0},
            {"listing_id": listing_id, "date": dt.date(2026, 8, 4), "rv_20d": 2.0, "rv_60d": 2.0},
        ],
    )
    rows = await get_volatility(pg_conn, listing_id=listing_id, start=dt.date(2026, 8, 4))
    assert len(rows) == 1
    assert rows[0]["rv_20d"] == 2.0
