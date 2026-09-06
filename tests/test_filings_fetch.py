"""SEC filings pipeline PostgreSQL tests."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl

from data.db.filings import get_checkpoint_date
from data.db.identifiers import get_or_create_identifier
from data.filings.fetch import run_daily_fetch
from data.sources.sec import Submission

COMPANYFACTS_FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "companyfacts_aapl.json"


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
                accession="000032019326000002",
                form_type="10-Q",
                filed_at="2026-08-30",
                period_of_report="2026-06-30",
                primary_document="quarterly.htm",
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

    def companyfacts(self, cik: str) -> dict[str, object]:
        return json.loads(COMPANYFACTS_FIXTURE.read_text(encoding="utf-8"))


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
    assert first["filings_parsed"] == 2
    assert second["filings_parsed"] == 0
    assert first["financial_issuers"] == 1
    assert first["financial_facts"] == 3
    assert second["financial_facts"] == 0
    assert (await (await pg_conn.execute("SELECT COUNT(*) FROM financials")).fetchone())[0] == 3
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
    assert failed["filings_skipped"] == 2
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") is None

    client.failing = False
    recovered = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )
    assert recovered["filings_parsed"] == 2
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )


class FactsOnlyFailureClient(FakeClient):
    """Serve filings normally but fail the company facts refresh."""

    def companyfacts(self, cik: str) -> dict[str, object]:
        raise RuntimeError("company facts unavailable")


async def test_company_facts_failure_does_not_hold_back_the_filings_checkpoint(
    pg_conn, tmp_path
) -> None:
    """Facts are a separate concern: only filing failures pin the checkpoint."""
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

    counts = await run_daily_fetch(
        pg_conn,
        FactsOnlyFailureClient(),
        run_date="2026-08-31",
        root=tmp_path,
        universe=universe,
    )

    assert counts["filings_parsed"] == 2
    assert counts["financials_skipped"] == 1
    assert counts["financial_issuers"] == 0
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )


class NonPeriodicClient(FakeClient):
    """Serve only an 8-K, and record every Company Facts request."""

    def __init__(self) -> None:
        self.companyfacts_calls = 0

    def submissions(self, cik: str) -> list[Submission]:
        return [
            Submission(
                accession="000032019326000003",
                form_type="8-K",
                filed_at="2026-08-30",
                period_of_report="2026-08-30",
                primary_document="report.htm",
            )
        ]

    def companyfacts(self, cik: str) -> dict[str, object]:
        self.companyfacts_calls += 1
        return super().companyfacts(cik)


async def test_company_facts_refresh_is_skipped_without_a_periodic_report(
    pg_conn, tmp_path
) -> None:
    """Company Facts only changes on periodic reports, so nothing else pulls it."""
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
    client = NonPeriodicClient()
    universe = pl.DataFrame({"ticker": ["AAPL"]})

    counts = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )

    assert counts["filings_parsed"] == 1
    assert client.companyfacts_calls == 0
    assert counts["financial_issuers"] == 0
    assert counts["financial_facts"] == 0
    assert (await (await pg_conn.execute("SELECT COUNT(*) FROM financials")).fetchone())[0] == 0
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )


async def test_company_facts_refresh_follows_a_periodic_report(pg_conn, tmp_path) -> None:
    """A 10-Q pulls facts once; a later run with nothing new pulls nothing."""
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

    class CountingClient(FakeClient):
        def __init__(self) -> None:
            self.companyfacts_calls = 0

        def companyfacts(self, cik: str) -> dict[str, object]:
            self.companyfacts_calls += 1
            return super().companyfacts(cik)

    client = CountingClient()
    universe = pl.DataFrame({"ticker": ["AAPL"]})

    first = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )
    second = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )

    assert first["financial_issuers"] == 1
    assert first["financial_facts"] == 3
    assert second["financial_issuers"] == 0
    assert client.companyfacts_calls == 1
