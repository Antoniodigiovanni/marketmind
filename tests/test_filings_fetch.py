"""Tests for the daily SEC filings fetch job (mocked client, tmp files).

Document/metadata filings are dual-written to DuckDB (asserted here as
before) and PostgreSQL (asserted via pg_conn) -- see data.filings.fetch's
module docstring. Ownership stays DuckDB-only.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from data.db.identifiers import get_or_create_identifier
from data.db.init import init_db
from data.filings.fetch import _ownership_xml_document, run_daily_fetch

# Not using `pytestmark = pytest.mark.asyncio` here (unlike the db repo test
# modules): this file mixes async fetch tests with two sync
# _ownership_xml_document tests, and pytest-asyncio's `asyncio_mode = "auto"`
# (pyproject.toml) already collects `async def` tests without it.


def _sub(accession, form_type, filed_at, period, doc) -> object:
    from data.sources.sec import Submission

    return Submission(accession, form_type, filed_at, period, doc)


class FakeClient:
    def __init__(self) -> None:
        self._subs = {
            "0000320193": [
                _sub("000032019326000045", "8-K", "2026-08-03", "2026-08-03", "form8k.htm"),
                _sub("000032019326000031", "10-Q", "2026-07-30", "2026-06-27", "q.htm"),
                _sub("000032019326000012", "4", "2026-05-02", "2026-05-02", "x1234.xml"),
            ]
        }
        self._docs = {
            "000032019326000045": "<html><body><p>Item 5.02 Changes</p></body></html>",
            "000032019326000031": "<html><body><p>Item 2. Results</p></body></html>",
            "000032019326000012": (
                "<ownershipDocument><documentType>4</documentType>"
                "<periodOfReport>2026-05-02</periodOfReport>"
                "<reportingOwner><reportingOwnerId><rptOwnerCik>0001003781</rptOwnerCik>"
                "<rptOwnerName>COOK TIMOTHY D</rptOwnerName></reportingOwnerId>"
                "<reportingOwnerRelationship><isOfficer>1</isOfficer>"
                "<officerTitle>CEO</officerTitle></reportingOwnerRelationship></reportingOwner>"
                "<nonDerivativeTable><nonDerivativeTransaction>"
                "<securityTitle>Common Stock</securityTitle>"
                "<transactionDate>2026-04-30</transactionDate>"
                "<transactionCoding><transactionCode>S</transactionCode></transactionCoding>"
                "<transactionAmounts><transactionShares>500</transactionShares>"
                "<transactionPricePerShare>180.25</transactionPricePerShare>"
                "</transactionAmounts>"
                "<postTransactionAmounts><sharesOwnedFollowingTransaction>9500"
                "</sharesOwnedFollowingTransaction></postTransactionAmounts>"
                "<ownershipNature><directOrIndirectOwnership>D</directOrIndirectOwnership>"
                "</ownershipNature></nonDerivativeTransaction></nonDerivativeTable>"
                "</ownershipDocument>"
            ),
        }

    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        return {"AAPL": {"cik": "0000320193", "name": "Apple Inc."}}

    def submissions(self, cik: str):
        return self._subs.get(cik, [])

    def all_submissions(self, cik: str):
        return self.submissions(cik)

    def download(self, cik: str, accession: str, doc: str) -> str:
        return self._docs[accession]

    def filing_index(self, cik: str, accession: str) -> list[dict[str, str]]:
        return []


class IndexClient:
    def __init__(self, files: list[dict[str, str]]) -> None:
        self._files = files

    def filing_index(self, cik: str, accession: str) -> list[dict[str, str]]:
        return self._files


def _seed_cik(conn) -> None:
    conn.execute(
        """
        INSERT INTO symbol_map (id_type, id_value, provider, symbol, name, status)
        VALUES ('CIK', '0000320193', 'sec', 'AAPL', 'Apple Inc.', 'valid')
        """
    )


@pytest.fixture()
def conn():
    connection = init_db(":memory:")
    _seed_cik(connection)
    yield connection
    connection.close()


async def test_run_daily_fetch_ingests_new_filings(conn, pg_conn, tmp_path) -> None:
    universe = pl.DataFrame({"ticker": ["AAPL"], "universe": ["sp500"]})
    counts = await run_daily_fetch(
        conn, pg_conn, FakeClient(), run_date="2026-08-05", root=tmp_path, universe=universe
    )

    assert counts == {
        "filings_fetched": 3,
        "filings_parsed": 3,
        "filings_skipped": 0,
        "insider_rows": 1,
        "form13f_rows": 0,
    }
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM insider_transactions").fetchone()[0] == 1

    articles = conn.execute("SELECT source FROM articles ORDER BY source").fetchall()
    assert articles == [("sec:10-Q",), ("sec:8-K",)]

    html_path = tmp_path / "filings" / "0000320193" / "000032019326000045.html"
    text_path = tmp_path / "filings" / "text" / "0000320193" / "000032019326000045.txt"
    assert html_path.exists()
    assert text_path.exists()
    assert "Item 5.02" in text_path.read_text(encoding="utf-8")

    run_row = conn.execute("SELECT job_type, filings_parsed FROM agent_runs").fetchone()
    assert run_row == ("filings", 3)

    from data.filings.checkpoint import has_checkpoint

    assert has_checkpoint(conn, "submissions:AAPL", "2026-08-05")

    # Postgres side: only the two document filings (8-K, 10-Q) dual-write;
    # the ownership filing (form 4) stays DuckDB-only in this cutover.
    pg_rows = await (
        await pg_conn.execute("SELECT accession, form_type FROM filings ORDER BY accession")
    ).fetchall()
    assert pg_rows == [
        ("000032019326000031", "10-Q"),
        ("000032019326000045", "8-K"),
    ]
    text_rows = await (await pg_conn.execute("SELECT COUNT(*) FROM filing_text")).fetchone()
    assert text_rows[0] == 2

    from data.db.filings import has_checkpoint as pg_has_checkpoint

    assert await pg_has_checkpoint(pg_conn, source="submissions:AAPL", key="2026-08-05")


async def test_run_daily_fetch_resolves_company_id(conn, pg_conn, tmp_path) -> None:
    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple Inc.",
        country="US",
        sector="Technology",
    )
    universe = pl.DataFrame({"ticker": ["AAPL"], "universe": ["sp500"]})
    await run_daily_fetch(
        conn, pg_conn, FakeClient(), run_date="2026-08-05", root=tmp_path, universe=universe
    )

    row = await (
        await pg_conn.execute(
            "SELECT company_id FROM filings WHERE accession = %s", ("000032019326000045",)
        )
    ).fetchone()
    assert row[0] == company_id


async def test_run_daily_fetch_reports_progress_per_ticker(conn, pg_conn, tmp_path) -> None:
    universe = pl.DataFrame({"ticker": ["AAPL", "MSFT"], "universe": ["sp500", "sp500"]})
    seen: list[tuple[int, int]] = []

    await run_daily_fetch(
        conn,
        pg_conn,
        FakeClient(),
        run_date="2026-08-05",
        root=tmp_path,
        universe=universe,
        progress=lambda done, total, counts: seen.append((done, total)),
    )

    assert seen == [(1, 2), (2, 2)]


async def test_run_daily_fetch_is_idempotent(conn, pg_conn, tmp_path) -> None:
    universe = pl.DataFrame({"ticker": ["AAPL"], "universe": ["sp500"]})
    first = await run_daily_fetch(
        conn, pg_conn, FakeClient(), run_date="2026-08-05", root=tmp_path, universe=universe
    )
    second = await run_daily_fetch(
        conn, pg_conn, FakeClient(), run_date="2026-08-05", root=tmp_path, universe=universe
    )

    assert first["filings_fetched"] == 3
    assert second["filings_fetched"] == 0
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 3
    pg_count = await (await pg_conn.execute("SELECT COUNT(*) FROM filings")).fetchone()
    assert pg_count[0] == 2


async def test_run_daily_fetch_isolates_failures(conn, pg_conn, tmp_path) -> None:
    client = FakeClient()

    def broken_download(cik: str, accession: str, doc: str) -> str:
        if accession == "000032019326000031":
            raise RuntimeError("boom")
        return client._docs[accession]

    client.download = broken_download  # type: ignore[method-assign]
    universe = pl.DataFrame({"ticker": ["AAPL"], "universe": ["sp500"]})

    counts = await run_daily_fetch(
        conn, pg_conn, client, run_date="2026-08-05", root=tmp_path, universe=universe
    )

    assert counts["filings_fetched"] == 3
    assert counts["filings_parsed"] == 2
    assert counts["filings_skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 2
    pg_count = await (await pg_conn.execute("SELECT COUNT(*) FROM filings")).fetchone()
    assert pg_count[0] == 1


def test_ownership_xml_document_prefers_root_xml() -> None:
    client = IndexClient(
        [
            {"name": "index.html", "type": "html"},
            {"name": "form4.xml", "type": "xml"},
        ]
    )
    assert (
        _ownership_xml_document(client, "0000320193", "acc1", "xslF345X06/form4.xml") == "form4.xml"
    )


def test_ownership_xml_document_falls_back_to_basename() -> None:
    client = IndexClient([])
    assert (
        _ownership_xml_document(client, "0000320193", "acc1", "xslF345X06/form4.xml") == "form4.xml"
    )
