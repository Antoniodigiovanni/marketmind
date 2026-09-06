"""SEC filings pipeline PostgreSQL tests."""

from __future__ import annotations

import datetime as dt

import polars as pl

from data.db.filings import get_checkpoint_date
from data.db.identifiers import get_or_create_identifier
from data.filings.fetch import run_daily_fetch
from data.sources.sec import Submission


class FakeClient:
    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        return {"AAPL": {"cik": "0000320193", "name": "Apple Inc."}}

    def submissions(self, cik: str) -> list[Submission]:
        return [
            Submission(
                accession="000032019326000001",
                form_type="8-K",
                filed_at="2026-08-30",
                period_of_report="2026-08-30",
                primary_document="report.htm",
            ),
            Submission(
                accession="000032019320000001",
                form_type="10-K",
                filed_at="2020-11-01",
                period_of_report="2020-09-30",
                primary_document="old.htm",
            ),
        ]

    def download(self, cik: str, accession: str, document: str) -> str:
        return "<html><body><h1>Results</h1><p>Revenue increased.</p></body></html>"


class FlakyClient(FakeClient):
    """Fail the current filing until explicitly recovered."""

    failing = True

    def download(self, cik: str, accession: str, document: str) -> str:
        if self.failing:
            raise RuntimeError("temporary archive failure")
        return super().download(cik, accession, document)


async def test_daily_filings_fetch_is_idempotent(pg_conn, tmp_path) -> None:
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
    universe = pl.DataFrame({"ticker": ["AAPL"]})
    first = await run_daily_fetch(
        pg_conn,
        FakeClient(),
        run_date="2026-08-31",
        root=tmp_path,
        universe=universe,
    )
    second = await run_daily_fetch(
        pg_conn,
        FakeClient(),
        run_date="2026-08-31",
        root=tmp_path,
        universe=universe,
    )
    assert first["filings_parsed"] == 1
    assert second["filings_parsed"] == 0
    assert (tmp_path / "filings" / "0000320193" / "000032019326000001.html").exists()
    assert not (tmp_path / "filings" / "0000320193" / "000032019320000001.html").exists()
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )


async def test_failed_filing_does_not_advance_checkpoint(pg_conn, tmp_path) -> None:
    """A transient document failure remains eligible on the next run."""
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
    client = FlakyClient()
    universe = pl.DataFrame({"ticker": ["AAPL"]})

    failed = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )
    assert failed["filings_skipped"] == 1
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") is None

    client.failing = False
    recovered = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )
    assert recovered["filings_parsed"] == 1
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )
