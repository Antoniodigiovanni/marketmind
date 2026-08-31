"""SEC filings pipeline PostgreSQL tests."""

from __future__ import annotations

import datetime as dt

import polars as pl

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
            )
        ]

    def download(self, cik: str, accession: str, document: str) -> str:
        return "<html><body><h1>Results</h1><p>Revenue increased.</p></body></html>"


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
