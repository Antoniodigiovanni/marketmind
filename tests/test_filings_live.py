"""Opt-in live EDGAR download smoke tests.

These tests hit the real SEC EDGAR endpoints and are excluded from the default
offline suite. Run them later with:

    RUN_LIVE_TESTS=1 uv run pytest tests/test_filings_live.py -v

EdgardClient respects SEC's rate limits (<=10 req/s) and sends a descriptive
User-Agent.
"""

from __future__ import annotations

import os

import polars as pl
import pytest

from data.db.init import init_db
from data.filings.fetch import _ownership_xml_document, run_daily_fetch
from data.filings.parse.html import html_to_text
from data.filings.parse.ownership import parse_ownership_xml
from data.filings.store import COVERED_FORMS, insert_filing
from data.sources.sec import EdgardClient, Submission

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("RUN_LIVE_TESTS"),
        reason="live EDGAR tests need network; set RUN_LIVE_TESTS=1 to run",
    ),
]

AAPL_CIK = "0000320193"
AAPL_TICKER = "AAPL"


def _newest_filing(client: EdgardClient, form_type: str) -> Submission | None:
    """Return the most recent filing of a form type for AAPL, if any."""
    for sub in client.submissions(AAPL_CIK):
        if sub.form_type == form_type:
            return sub
    return None


def test_download_latest_8k_text() -> None:
    client = EdgardClient()
    filing = _newest_filing(client, "8-K")
    if filing is None:
        pytest.skip("AAPL has no recent 8-K")

    raw = client.download(AAPL_CIK, filing.accession, filing.primary_document)
    assert len(raw) > 1000
    assert len(html_to_text(raw)) > 100


def test_download_and_parse_latest_form4() -> None:
    client = EdgardClient()
    filing = _newest_filing(client, "4")
    if filing is None:
        pytest.skip("AAPL has no recent Form 4")

    document = _ownership_xml_document(client, AAPL_CIK, filing.accession, filing.primary_document)
    raw = client.download(AAPL_CIK, filing.accession, document)
    transactions, holdings = parse_ownership_xml(raw, ticker=AAPL_TICKER)
    assert transactions or holdings


def test_live_daily_fetch_end_to_end(tmp_path) -> None:
    """Run the daily fetch for AAPL against live EDGAR, newest filings only."""
    client = EdgardClient()
    conn = init_db(":memory:")
    try:
        submissions = client.submissions(AAPL_CIK)
        covered = [s for s in submissions if s.form_type in COVERED_FORMS]
        if not covered:
            pytest.skip("AAPL has no covered filings")

        # Seed the DB with everything except the newest filing per form type,
        # so the live run downloads only a handful of documents.
        newest_per_type: dict[str, Submission] = {}
        for sub in covered:
            newest_per_type.setdefault(sub.form_type, sub)
        for sub in covered:
            if any(sub is kept for kept in newest_per_type.values()):
                continue
            insert_filing(
                conn,
                cik=AAPL_CIK,
                accession_number=sub.accession,
                form_type=sub.form_type,
                filed_at=sub.filed_at,
                period_of_report=sub.period_of_report,
                primary_document=sub.primary_document,
                url="https://example.com/seeded",
                ticker=AAPL_TICKER,
            )

        counts = run_daily_fetch(
            conn,
            client,
            run_date="2026-08-05",
            root=tmp_path,
            universe=pl.DataFrame({"ticker": [AAPL_TICKER], "universe": ["sp500"]}),
        )

        assert counts["filings_fetched"] >= 1
        assert counts["filings_parsed"] >= 1
        assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] >= 1
    finally:
        conn.close()
