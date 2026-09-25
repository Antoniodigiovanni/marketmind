"""PostgreSQL-only SEC filing backfill tests."""

from __future__ import annotations

import datetime as dt

from data.db.filings import has_checkpoint
from data.db.identifiers import get_or_create_identifier
from data.filings.backfill import backfill_13f, backfill_issuer
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


async def test_13f_backfill_retries_a_failed_filing(pg_conn, tmp_path, monkeypatch) -> None:
    """Do not checkpoint a transient 13F failure before it can be retried."""
    from data.filings import backfill as backfill_module

    accession = "000106798326000001"

    def fake_filings(client, *, start: str, end: str):
        yield "0001067983", accession, "2026-08-14", "Berkshire Hathaway"

    calls = 0

    async def flaky_process(*args, **kwargs) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary archive failure")
        return 2

    monkeypatch.setattr(backfill_module, "iter_13f_filings", fake_filings)
    monkeypatch.setattr(backfill_module, "_process_13f", flaky_process)

    failed = await backfill_13f(
        pg_conn,
        object(),  # type: ignore[arg-type]
        tmp_path,
        start="2026-08-01",
        end="2026-08-31",
    )
    assert failed["skipped"] == 1
    assert not await has_checkpoint(pg_conn, source="13f", key=accession)

    recovered = await backfill_13f(
        pg_conn,
        object(),  # type: ignore[arg-type]
        tmp_path,
        start="2026-08-01",
        end="2026-08-31",
    )
    assert recovered["13f_filings"] == 1
    assert recovered["13f_rows"] == 2
    assert await has_checkpoint(pg_conn, source="13f", key=accession)
