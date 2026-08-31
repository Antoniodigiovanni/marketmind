"""MCP validation and point-in-time reads for structured financials."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

import server.mcp as mcp_mod
from data.db.financials import FinancialObservation, insert_financials
from data.db.identifiers import get_or_create_identifier


class _FakePool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def connection(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patch_pool(monkeypatch: pytest.MonkeyPatch, pg_conn) -> None:
    async def fake_pool():
        return _FakePool(pg_conn)

    monkeypatch.setattr(mcp_mod, "_pg_pool", fake_pool)


async def test_get_financials_returns_latest_value_visible_at_cutoff(monkeypatch, pg_conn) -> None:
    _patch_pool(monkeypatch, pg_conn)
    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )
    base = {
        "taxonomy": "us-gaap",
        "concept": "Revenues",
        "unit": "USD",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "period_start": dt.date(2024, 9, 29),
        "period_end": dt.date(2025, 9, 27),
        "form_type": "10-K",
        "frame": "CY2025",
    }
    await insert_financials(
        pg_conn,
        company_id=company_id,
        observations=[
            FinancialObservation(
                **base,
                filed_at=dt.date(2025, 10, 31),
                accession_number="original",
                value=Decimal("100"),
            ),
            FinancialObservation(
                **base,
                filed_at=dt.date(2026, 10, 30),
                accession_number="restated",
                value=Decimal("101"),
            ),
        ],
    )

    original = await mcp_mod.get_financials("AAPL", ["Revenues"], "2025-12-31")
    restated = await mcp_mod.get_financials("AAPL", ["Revenues"], "2026-12-31")

    assert original[0]["value"] == 100
    assert original[0]["accession_number"] == "original"
    assert restated[0]["value"] == 101
    assert restated[0]["accession_number"] == "restated"
    assert await mcp_mod.get_financials("NOPE", ["Revenues"], "2026-12-31") == []


@pytest.mark.parametrize(
    ("ticker", "concepts", "as_of"),
    [
        ("", ["Revenues"], "2026-01-01"),
        (" AAPL", ["Revenues"], "2026-01-01"),
        ("AAPL", [], "2026-01-01"),
        ("AAPL", ["us-gaap:Revenues"], "2026-01-01"),
        ("AAPL", ["Revenues", "Revenues"], "2026-01-01"),
        ("AAPL", ["Revenues"], "20260101"),
        ("AAPL", ["Revenues"], "2026-02-30"),
    ],
)
async def test_get_financials_rejects_invalid_inputs(ticker, concepts, as_of) -> None:
    with pytest.raises(ValueError):
        await mcp_mod.get_financials(ticker, concepts, as_of)
