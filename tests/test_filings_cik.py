"""Tests for CIK mapping sync into symbol_map."""

from __future__ import annotations

from data.db.init import init_db
from data.filings.cik import cik_for_ticker, ensure_cik_map


class FakeClient:
    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        return {
            "AAPL": {"cik": "0000320193", "name": "Apple Inc."},
            "MSFT": {"cik": "0000789019", "name": "MICROSOFT CORP"},
        }


def test_ensure_cik_map_upserts_missing_tickers() -> None:
    conn = init_db(":memory:")
    try:
        count = ensure_cik_map(conn, FakeClient(), ["AAPL", "MSFT", "NOPE"])
        assert count == 2
        assert cik_for_ticker(conn, "AAPL") == "0000320193"
        assert cik_for_ticker(conn, "MSFT") == "0000789019"
        assert cik_for_ticker(conn, "NOPE") is None
    finally:
        conn.close()


def test_ensure_cik_map_is_idempotent() -> None:
    conn = init_db(":memory:")
    try:
        assert ensure_cik_map(conn, FakeClient(), ["AAPL"]) == 1
        assert ensure_cik_map(conn, FakeClient(), ["AAPL"]) == 0
    finally:
        conn.close()
