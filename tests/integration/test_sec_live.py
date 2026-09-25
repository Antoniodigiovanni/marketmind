"""Live contract checks for the public SEC EDGAR endpoints."""

from __future__ import annotations

import os
from typing import Any

import pytest

from data.sources.sec import COMPANYFACTS_URL, SUBMISSIONS_URL, EdgardClient

AAPL_CIK = "0000320193"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TESTS") != "1",
        reason="set RUN_LIVE_TESTS=1 to call the live SEC endpoints",
    ),
]


def _historical_files(payload: dict[str, Any]) -> list[dict[str, Any]]:
    filings = payload.get("filings")
    assert isinstance(filings, dict), "current submissions response has no filings object"
    files = filings.get("files")
    assert isinstance(files, list), "current submissions response has no files array"
    assert files, "contract issuer no longer exposes a historical submissions page"
    assert all(isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in files)
    return files


def test_live_current_and_historical_submissions_contract() -> None:
    """Parse both SEC submissions response shapes through the production client."""
    client = EdgardClient()
    current_url = SUBMISSIONS_URL.format(cik=AAPL_CIK)
    current = client.get_json(current_url)

    assert isinstance(current, dict)
    recent = client._parse_recent(current)
    assert recent
    assert all(item.accession and item.form_type and item.filed_at for item in recent)

    first_page = _historical_files(current)[0]
    historical = client.get_json("https://data.sec.gov/submissions/" + str(first_page["name"]))
    assert isinstance(historical, dict)
    assert "filings" not in historical, "historical SEC contract unexpectedly became nested"
    older = client._parse_recent(historical)
    assert older
    assert all(item.accession and item.form_type and item.filed_at for item in older)


def test_live_company_facts_contract() -> None:
    """Validate the SEC Company Facts envelope used by financial ingestion."""
    payload = EdgardClient().get_json(COMPANYFACTS_URL.format(cik=AAPL_CIK))

    assert isinstance(payload, dict)
    assert str(payload.get("cik")) == str(int(AAPL_CIK))
    assert isinstance(payload.get("entityName"), str) and payload["entityName"]
    assert isinstance(payload.get("facts"), dict) and payload["facts"]
