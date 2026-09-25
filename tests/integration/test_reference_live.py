"""Live contract checks for reference data and company metadata."""

from __future__ import annotations

import pytest

from data.sources.gleif import GleifClient
from data.sources.website import fetch_official_website_url
from data.sources.wikipedia import fetch_wikipedia_context
from data.sources.xbrlorg import XbrlorgClient

pytestmark = pytest.mark.live


def test_live_gleif_contract() -> None:
    """Parse GLEIF JSON-API records for a known legal entity."""
    candidates = GleifClient().search("Apple Inc", "US")

    assert candidates
    assert all(item.lei and item.legal_name and item.country and item.status for item in candidates)
    assert all(item.country == "US" for item in candidates)


def test_live_xbrlorg_contract() -> None:
    """Parse one page of the filings.xbrl.org JSON-API."""
    filings = XbrlorgClient().get_filings("IT", max_pages=1)

    assert filings
    assert all(item.fxo_id and item.lei and item.entity_name for item in filings)
    assert all(item.country == "IT" and item.filing_system == "ESEF" for item in filings)
    assert all(item.package_url.startswith("https://") for item in filings)


def test_live_wikipedia_contract() -> None:
    """Validate the Wikipedia summary envelope used for company profiles."""
    context = fetch_wikipedia_context("Apple Inc.")

    assert context["extract"]
    assert context["wikibase_item"] == "Q312"
    assert str(context["page_url"]).startswith("https://")


def test_live_wikidata_contract() -> None:
    """Validate Wikidata official-website claims for a known company."""
    url = fetch_official_website_url("Q312")

    assert url is not None
    assert url.startswith("https://")
