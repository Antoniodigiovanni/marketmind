"""Offline parsing, ingestion, and point-in-time tests for SEC Company Facts."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from data.db.financials import FinancialObservation, get_financials_as_of, insert_financials
from data.db.identifiers import get_or_create_identifier
from data.filings.companyfacts import ingest_companyfacts, parse_companyfacts

FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "companyfacts_aapl.json"


class FakeCompanyFactsClient:
    """Fixture-backed source with no network access."""

    def __init__(self) -> None:
        self.ciks: list[str] = []

    def companyfacts(self, cik: str) -> dict[str, object]:
        self.ciks.append(cik)
        return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_companyfacts_preserves_restatements_and_instant_facts() -> None:
    observations = parse_companyfacts(json.loads(FIXTURE.read_text(encoding="utf-8")))

    assert len(observations) == 3
    revenues = [row for row in observations if row.concept == "Revenues"]
    assert [row.value for row in revenues] == [Decimal("100000000000"), Decimal("101000000000")]
    assert revenues[0].accession_number == "000032019325000100"
    assets = next(row for row in observations if row.concept == "Assets")
    assert assets.period_start is None
    assert assets.frame == "CY2025Q3I"


@pytest.mark.parametrize("payload", [{}, {"facts": []}, {"facts": {"us-gaap": []}}])
def test_parse_companyfacts_rejects_malformed_containers(payload) -> None:
    with pytest.raises(ValueError):
        parse_companyfacts(payload)


async def _company(pg_conn):
    return await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )


async def test_ingest_is_idempotent_and_pit_read_hides_future_restatement(pg_conn) -> None:
    company_id = await _company(pg_conn)
    client = FakeCompanyFactsClient()

    assert await ingest_companyfacts(pg_conn, client, company_id=company_id, cik="0000320193") == 3
    assert await ingest_companyfacts(pg_conn, client, company_id=company_id, cik="0000320193") == 0

    before = await get_financials_as_of(
        pg_conn,
        company_id=company_id,
        concepts=["Revenues", "Assets"],
        as_of=dt.date(2025, 12, 31),
    )
    assert {row["concept"]: row["value"] for row in before} == {
        "Assets": Decimal("350000000000"),
        "Revenues": Decimal("100000000000"),
    }

    after = await get_financials_as_of(
        pg_conn,
        company_id=company_id,
        concepts=["Revenues"],
        as_of=dt.date(2026, 12, 31),
    )
    assert after[0]["value"] == Decimal("101000000000")
    assert after[0]["filed_at"] == dt.date(2026, 10, 30)


async def test_period_start_distinguishes_quarter_and_year_to_date(pg_conn) -> None:
    company_id = await _company(pg_conn)
    common = {
        "taxonomy": "us-gaap",
        "concept": "Revenues",
        "unit": "USD",
        "fiscal_year": 2026,
        "fiscal_period": "Q2",
        "period_end": dt.date(2026, 3, 28),
        "filed_at": dt.date(2026, 5, 1),
        "accession_number": "quarterly",
        "form_type": "10-Q",
        "frame": None,
    }
    await insert_financials(
        pg_conn,
        company_id=company_id,
        observations=[
            FinancialObservation(**common, period_start=dt.date(2025, 12, 28), value=Decimal("25")),
            FinancialObservation(**common, period_start=dt.date(2025, 9, 28), value=Decimal("52")),
        ],
    )

    rows = await get_financials_as_of(
        pg_conn,
        company_id=company_id,
        concepts=["Revenues"],
        as_of=dt.date(2026, 5, 1),
    )
    assert {row["value"] for row in rows} == {Decimal("25"), Decimal("52")}
