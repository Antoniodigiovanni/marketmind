"""Tests for the one-time filings backfill (mocked client, resumable)."""

from __future__ import annotations

import pytest

from data.db.init import init_db
from data.filings.backfill import backfill_13f, backfill_issuer
from data.filings.checkpoint import has_checkpoint
from data.sources.sec import Submission


class FakeClient:
    def __init__(self) -> None:
        self._subs = {
            "0000320193": [
                Submission("000032019320000001", "10-K", "2020-11-01", "2020-09-30", "old.htm"),
                Submission("000032019326000031", "10-Q", "2026-07-30", "2026-06-27", "q.htm"),
                Submission("000032019326000012", "4", "2026-05-02", "2026-05-02", "x.xml"),
            ]
        }
        self._docs = {
            "000032019320000001": "<html><body><p>Old 10-K</p></body></html>",
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
        self.search_pages: list[dict] = []
        self._indexes: dict[str, list[dict[str, str]]] = {}

    def all_submissions(self, cik: str):
        return self._subs.get(cik, [])

    def download(self, cik: str, accession: str, doc: str) -> str:
        return self._docs.get(f"{accession}-{doc}") or self._docs[accession]

    def filing_index(self, cik: str, accession: str) -> list[dict[str, str]]:
        return self._indexes.get(accession, [])

    def search_index(
        self, *, query: str, forms: str, start: str, end: str, cursor: str | None = None
    ):
        if not self.search_pages:
            return {"hits": {"total": {"value": 0}, "hits": []}, "cursor": None}
        return self.search_pages.pop(0)


@pytest.fixture()
def conn():
    connection = init_db(":memory:")
    yield connection
    connection.close()


def test_backfill_issuer_metadata_and_full_text(conn, tmp_path) -> None:
    counts = backfill_issuer(
        conn,
        FakeClient(),
        tmp_path,
        ticker="AAPL",
        cik="0000320193",
        full_text_since="2025-01-01",
        checkpoint_date="2026-08-05",
    )

    assert counts["filings_fetched"] == 3
    assert counts["filings_parsed"] == 3
    old = conn.execute(
        "SELECT text_path FROM filings WHERE accession_number = '000032019320000001'"
    ).fetchone()
    recent = conn.execute(
        "SELECT text_path FROM filings WHERE accession_number = '000032019326000031'"
    ).fetchone()
    assert old[0] is None
    assert recent[0] is not None
    assert (tmp_path / recent[0]).exists()
    assert conn.execute("SELECT COUNT(*) FROM insider_transactions").fetchone()[0] == 1
    assert has_checkpoint(conn, "backfill:issuer:AAPL", "2026-08-05")


def test_backfill_13f_parses_and_resumes(conn, tmp_path) -> None:
    infotable = (
        "<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
        "<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>"
        "<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
        "</shrsOrPrnAmt><investmentDiscretion>SOLE</investmentDiscretion>"
        "<votingAuthority><Sole>10</Sole><Shared>0</Shared><None>0</None>"
        "</votingAuthority></infoTable></informationTable>"
    )
    cover = (
        "<edgarSubmission><formData><coverPage>"
        "<reportCalendarOrQuarter>2026-03-31</reportCalendarOrQuarter>"
        "</coverPage></formData></edgarSubmission>"
    )
    client = FakeClient()
    client.search_pages = [
        {
            "hits": {
                "total": {"value": 3},
                "hits": [
                    {
                        "_source": {
                            "cik": "0000123456",
                            "accessionNumber": "0000123456-26-000101",
                            "filingDate": "2026-05-01",
                            "entityName": "Test Fund",
                        }
                    },
                    {
                        "_source": {
                            "cik": "0000123456",
                            "accessionNumber": "0000123456-26-000102",
                            "filingDate": "2026-05-01",
                            "entityName": "No Table Fund",
                        }
                    },
                ],
            },
            "cursor": "abc",
        },
        {
            "hits": {
                "total": {"value": 3},
                "hits": [
                    {
                        "_source": {
                            "cik": "0000123456",
                            "accessionNumber": "0000123456-26-000103",
                            "filingDate": "2026-05-01",
                            "entityName": "Test Fund",
                        }
                    }
                ],
            },
            "cursor": None,
        },
    ]
    client._indexes = {
        "000012345626000101": [
            {"name": "form13fInfoTable.xml", "type": "xml"},
            {"name": "form13f.xml", "type": "xml"},
        ],
        "000012345626000102": [{"name": "primary.xml", "type": "xml"}],
        "000012345626000103": [
            {"name": "form13fInfoTable.xml", "type": "xml"},
            {"name": "form13f.xml", "type": "xml"},
        ],
    }
    client._docs.update(
        {
            "000012345626000101": infotable,
            "000012345626000103": infotable,
            "000012345626000101-form13f.xml": cover,
            "000012345626000103-form13f.xml": cover,
        }
    )

    first = backfill_13f(conn, client, tmp_path, start="2026-01-01", end="2026-03-31")
    second = backfill_13f(conn, client, tmp_path, start="2026-01-01", end="2026-03-31")

    assert first == {"13f_filings": 2, "13f_rows": 2, "skipped": 1}
    assert second == {"13f_filings": 0, "13f_rows": 0, "skipped": 0}
    assert conn.execute("SELECT COUNT(*) FROM form13f_filings").fetchone()[0] == 2
    assert has_checkpoint(conn, "13f", "000012345626000102")
