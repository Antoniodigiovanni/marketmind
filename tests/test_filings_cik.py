"""PostgreSQL CIK mapping tests."""

from __future__ import annotations

import datetime as dt

from data.db.identifiers import get_or_create_identifier
from data.filings.cik import cik_for_ticker, ensure_cik_map


class FakeClient:
    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        return {"AAPL": {"cik": "0000320193", "name": "Apple Inc."}}


async def test_cik_mapping_round_trip(pg_conn) -> None:
    await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )
    assert await ensure_cik_map(pg_conn, FakeClient(), ["AAPL", "NOPE"]) == 1
    assert await cik_for_ticker(pg_conn, "AAPL") == "0000320193"
    assert await ensure_cik_map(pg_conn, FakeClient(), ["AAPL"]) == 0
