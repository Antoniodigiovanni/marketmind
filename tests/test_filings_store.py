"""Tests for filings store write helpers (in-memory DuckDB + tmp parquet)."""

from __future__ import annotations

import pytest

from data.db.init import init_db
from data.filings.checkpoint import has_checkpoint, mark_checkpoint
from data.filings.parse.form13f import parse_13f_infotable
from data.filings.parse.ownership import InsiderHolding, InsiderTransaction
from data.filings.store import (
    append_13f_holdings,
    create_filing_article,
    filing_exists,
    insert_filing,
    insert_insider_rows,
    log_run,
)


@pytest.fixture()
def conn():
    connection = init_db(":memory:")
    yield connection
    connection.close()


def _seed_article(conn) -> str:
    article_id = "art-1"
    conn.execute(
        """
        INSERT INTO articles (id, url_hash, content_hash, url, source, ticker,
                              published_at, fetched_at, status)
        VALUES (?, 'h', 'c', 'https://example.com', 'reuters', 'AAPL',
                '2026-01-01 09:00:00+00', '2026-01-01 10:00:00+00', 'processed')
        """,
        [article_id],
    )
    return article_id


def test_insert_filing_and_exists(conn) -> None:
    filing_id = insert_filing(
        conn,
        cik="0000320193",
        accession_number="000032019326000045",
        form_type="8-K",
        filed_at="2026-08-03",
        period_of_report="2026-08-03",
        primary_document="form8k.htm",
        url="https://www.sec.gov/Archives/edgar/data/0000320193/000032019326000045/form8k.htm",
        ticker="AAPL",
    )
    assert filing_exists(conn, "0000320193", "000032019326000045")
    assert not filing_exists(conn, "0000320193", "000000000000000000")
    row = conn.execute("SELECT status FROM filings WHERE id = ?", [filing_id]).fetchone()
    assert row[0] == "fetched"


def test_create_filing_article_links_and_sets_status(conn) -> None:
    filing_id = insert_filing(
        conn,
        cik="0000320193",
        accession_number="000032019326000045",
        form_type="8-K",
        filed_at="2026-08-03",
        period_of_report=None,
        primary_document="form8k.htm",
        url="https://www.sec.gov/Archives/edgar/data/x",
        ticker="AAPL",
    )
    article_id = create_filing_article(
        conn,
        filing_id=filing_id,
        ticker="AAPL",
        form_type="8-K",
        filed_at="2026-08-03",
        url="https://www.sec.gov/Archives/edgar/data/x",
        text_hash="abc",
    )

    article = conn.execute(
        "SELECT source, status FROM articles WHERE id = ?", [article_id]
    ).fetchone()
    assert article[0] == "sec:8-K"
    filing = conn.execute(
        "SELECT article_id, status FROM filings WHERE id = ?", [filing_id]
    ).fetchone()
    assert filing[0] == article_id
    assert filing[1] == "facts_pending"


def test_insert_insider_rows(conn) -> None:
    filing_id = insert_filing(
        conn,
        cik="0000320193",
        accession_number="a1",
        form_type="4",
        filed_at="2026-05-02",
        period_of_report=None,
        primary_document="x.xml",
        url="https://example.com",
        ticker="AAPL",
    )
    tx = InsiderTransaction(
        ticker="AAPL",
        owner_name="COOK TIMOTHY D",
        owner_cik="0001003781",
        owner_title="CEO",
        security_title="Common Stock",
        transaction_date="2026-04-30",
        deemed_execution_date=None,
        transaction_code="S",
        shares_transacted=500.0,
        price_per_share=180.25,
        shares_held_after=9500.0,
        direct_or_indirect="D",
    )
    holding = InsiderHolding(
        ticker="AAPL",
        owner_name="COOK TIMOTHY D",
        owner_cik="0001003781",
        owner_title="CEO",
        security_title="Common Stock",
        ref_date="2026-05-02",
        shares_held_after=9500.0,
        direct_or_indirect="D",
        form_type="4",
    )
    n_tx, n_h = insert_insider_rows(conn, filing_id, [tx], [holding])

    assert (n_tx, n_h) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM insider_transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM insider_holdings").fetchone()[0] == 1


def test_append_13f_holdings_partitions_by_quarter(tmp_path) -> None:
    raw = (
        "<informationTable><infoTable><nameOfIssuer>A</nameOfIssuer>"
        "<titleOfClass>COM</titleOfClass><cusip>037833100</cusip>"
        "<value>100</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt>"
        "<sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
        "<investmentDiscretion>SOLE</investmentDiscretion>"
        "<votingAuthority><Sole>10</Sole><Shared>0</Shared><None>0</None>"
        "</votingAuthority></infoTable></informationTable>"
    )
    df = parse_13f_infotable(raw, report_period="2026-03-31", filer_cik="1", filer_name=None)
    path1 = append_13f_holdings(df, root=tmp_path, report_period="2026-03-31")
    path2 = append_13f_holdings(df, root=tmp_path, report_period="2026-03-31")

    assert path1 == path2
    assert path1.name == "Q1.parquet"
    assert path1.parent.name == "2026"
    assert path1.exists()
    assert len(__import__("polars").read_parquet(path1)) == 2


def test_checkpoint_round_trip(conn) -> None:
    assert not has_checkpoint(conn, "13f", "ACC1")
    mark_checkpoint(conn, "13f", "ACC1")
    assert has_checkpoint(conn, "13f", "ACC1")


def test_log_run_writes_filings_row(conn) -> None:
    import datetime as dt

    log_run(
        conn,
        job_type="filings",
        run_date="2026-08-05",
        started_at=dt.datetime.now(dt.UTC),
        filings_fetched=3,
        filings_parsed=2,
        filings_skipped=1,
        insider_rows=4,
        form13f_rows=5,
    )
    row = conn.execute(
        "SELECT job_type, filings_parsed, insider_rows, form13f_rows FROM agent_runs"
    ).fetchone()
    assert row == ("filings", 2, 4, 5)
