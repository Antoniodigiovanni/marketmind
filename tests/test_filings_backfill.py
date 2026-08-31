"""PostgreSQL-only SEC filing backfill tests."""

from __future__ import annotations

import datetime as dt

from data.db.filings import has_checkpoint
from data.db.identifiers import get_or_create_identifier
from data.filings.backfill import backfill_issuer
from data.sources.sec import Submission


class FakeClient:
    """Minimal historical-submissions client."""

    def all_submissions(self, cik: str) -> list[Submission]:
        return [
            Submission("old", "10-K", "2020-11-01", "2020-09-30", "old.htm"),
            Submission("recent", "10-Q", "2026-07-30", "2026-06-27", "recent.htm"),
            Submission("owner", "4", "2026-05-02", "2026-05-02", "owner.xml"),
        ]

    def filing_index(self, cik: str, accession: str) -> list[dict[str, str]]:
        return [{"name": "owner.xml", "type": "xml"}] if accession == "owner" else []

    def download(self, cik: str, accession: str, document: str) -> str:
        if accession != "owner":
            return f"<html><body><p>{accession}</p></body></html>"
        return (
            "<ownershipDocument><documentType>4</documentType>"
            "<periodOfReport>2026-05-02</periodOfReport><reportingOwner>"
            "<reportingOwnerId><rptOwnerName>TEST OWNER</rptOwnerName></reportingOwnerId>"
            "</reportingOwner><nonDerivativeTable><nonDerivativeTransaction>"
            "<securityTitle>Common Stock</securityTitle>"
            "<transactionDate>2026-04-30</transactionDate>"
            "<transactionCoding><transactionCode>S</transactionCode></transactionCoding>"
            "<transactionAmounts><transactionShares>500</transactionShares>"
            "<transactionPricePerShare>180.25</transactionPricePerShare>"
            "</transactionAmounts></nonDerivativeTransaction></nonDerivativeTable>"
            "</ownershipDocument>"
        )


async def test_issuer_backfill_is_postgres_only_and_resumable(pg_conn, tmp_path) -> None:
    """Store metadata, recent text, ownership rows, and checkpoint once."""
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

    first = await backfill_issuer(
        pg_conn,
        FakeClient(),
        tmp_path,
        ticker="AAPL",
        cik="0000320193",
        full_text_since="2025-01-01",
        checkpoint_date="2026-08-31",
    )
    second = await backfill_issuer(
        pg_conn,
        FakeClient(),
        tmp_path,
        ticker="AAPL",
        cik="0000320193",
        full_text_since="2025-01-01",
        checkpoint_date="2026-08-31",
    )

    assert first["filings_parsed"] == 3
    assert first["insider_rows"] == 1
    assert second["filings_fetched"] == 0
    assert (await (await pg_conn.execute("SELECT COUNT(*) FROM filings")).fetchone())[0] == 3
    assert (await (await pg_conn.execute("SELECT COUNT(*) FROM filing_text")).fetchone())[0] == 1
    assert await has_checkpoint(pg_conn, source="backfill:issuer:AAPL", key="2026-08-31")
