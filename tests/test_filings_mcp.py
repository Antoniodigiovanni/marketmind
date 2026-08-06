"""Tests for filings MCP tools (file-backed DuckDB, no live calls)."""

from __future__ import annotations

import duckdb
import pytest

import server.mcp as mcp_mod
from data.db.init import init_db
from data.filings.store import insert_filing, insert_filing_text


@pytest.fixture()
def db_file(tmp_path) -> str:
    path = tmp_path / "mcp_filings.duckdb"
    connection = init_db(path)
    connection.close()
    return str(path)


def _seed_filing(
    conn, *, accession: str, form_type: str, filed_at: str, ticker: str = "AAPL"
) -> str:
    return insert_filing(
        conn,
        cik="0000320193",
        accession_number=accession,
        form_type=form_type,
        filed_at=filed_at,
        period_of_report=filed_at if form_type == "8-K" else "2026-06-27",
        primary_document=f"{accession}.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/0000320193/{accession}/{accession}.htm",
        ticker=ticker,
    )


def test_get_filings_filters_and_orders(monkeypatch, db_file) -> None:
    conn = duckdb.connect(db_file)
    _seed_filing(conn, accession="a1", form_type="8-K", filed_at="2026-08-03")
    _seed_filing(conn, accession="a2", form_type="10-Q", filed_at="2026-07-30")
    conn.close()
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))

    rows = mcp_mod.get_filings("AAPL", form_types=["8-K"])
    assert len(rows) == 1
    assert rows[0]["form_type"] == "8-K"
    assert rows[0]["filed_at"] == "2026-08-03"

    window = mcp_mod.get_filings("AAPL", filed_from="2026-08-01", filed_to="2026-08-03")
    assert [r["accession_number"] for r in window] == ["a1"]

    all_rows = mcp_mod.get_filings("AAPL")
    assert [r["accession_number"] for r in all_rows] == ["a1", "a2"]


def test_get_filings_empty(monkeypatch, db_file) -> None:
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))
    assert mcp_mod.get_filings("NOPE") == []


def test_search_filings_returns_snippets(monkeypatch, db_file) -> None:
    conn = duckdb.connect(db_file)
    filing_id = _seed_filing(conn, accession="a1", form_type="10-K", filed_at="2026-01-15")
    insert_filing_text(conn, filing_id, "Apple faces intense competition in phones.")
    conn.close()
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))

    hits = mcp_mod.search_filings(
        "competition", ticker="AAPL", form_types=["10-K"], filed_from="2026-01-01"
    )

    assert len(hits) == 1
    assert hits[0]["ticker"] == "AAPL"
    assert hits[0]["form_type"] == "10-K"
    assert "competition" in hits[0]["snippet"].lower()

    multi = mcp_mod.search_filings("competition phones")
    assert len(multi) == 1
    assert mcp_mod.search_filings("competition bonds") == []


def test_search_filings_empty_db_and_blank_query(monkeypatch, db_file) -> None:
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))
    assert mcp_mod.search_filings("anything") == []
    assert mcp_mod.search_filings("   ") == []


def test_get_insider_transactions_and_holdings(monkeypatch, db_file) -> None:
    conn = duckdb.connect(db_file)
    filing_id = _seed_filing(conn, accession="a3", form_type="4", filed_at="2026-05-02")
    conn.execute(
        """
        INSERT INTO insider_transactions (id, filing_id, ticker, owner_name, security_title,
            transaction_date, transaction_code, shares_transacted, price_per_share,
            shares_held_after, direct_or_indirect)
        VALUES ('tx1', ?, 'AAPL', 'COOK TIMOTHY D', 'Common Stock', '2026-04-30',
                'S', 500, 180.25, 9500, 'D')
        """,
        [filing_id],
    )
    conn.execute(
        """
        INSERT INTO insider_holdings (id, filing_id, ticker, owner_name, security_title,
            ref_date, shares_held_after, direct_or_indirect, form_type)
        VALUES ('h1', ?, 'AAPL', 'COOK TIMOTHY D', 'Common Stock', '2026-05-02',
                9500, 'D', '4')
        """,
        [filing_id],
    )
    conn.close()
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))

    txs = mcp_mod.get_insider_transactions("AAPL", since="2026-04-01")
    assert txs[0]["transaction_code"] == "S"
    assert txs[0]["shares_transacted"] == 500.0
    assert mcp_mod.get_insider_transactions("AAPL", since="2026-06-01") == []

    holdings = mcp_mod.get_insider_holdings("AAPL", as_of="2026-05-02")
    assert holdings[0]["shares_held_after"] == 9500.0
    assert mcp_mod.get_insider_holdings("AAPL", as_of="2026-05-01") == []


def test_get_13f_holdings_reads_partitions(monkeypatch, tmp_path, db_file) -> None:
    from data.filings.parse.form13f import parse_13f_infotable
    from data.filings.store import append_13f_holdings

    raw = (
        "<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
        "<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>"
        "<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
        "</shrsOrPrnAmt><investmentDiscretion>SOLE</investmentDiscretion>"
        "<votingAuthority><Sole>10</Sole><Shared>0</Shared><None>0</None>"
        "</votingAuthority></infoTable></informationTable>"
    )
    df = parse_13f_infotable(raw, report_period="2026-03-31", filer_cik="1", filer_name="Fund A")
    append_13f_holdings(df, root=tmp_path, report_period="2026-03-31")
    monkeypatch.setattr(mcp_mod, "FILINGS_13F_GLOB", str(tmp_path / "13f" / "**" / "*.parquet"))
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))

    rows = mcp_mod.get_13f_holdings("037833100")
    assert len(rows) == 1
    assert rows[0]["filer_name"] == "Fund A"
    assert rows[0]["value_usd"] == 100_000.0

    assert mcp_mod.get_13f_holdings("037833100", as_of="2026-01-01") == []
    assert mcp_mod.get_13f_holdings("037833100", min_value=200_000.0) == []
    assert len(mcp_mod.get_13f_holdings("037833100", min_value=50_000.0)) == 1


def test_get_13f_holdings_no_partitions(monkeypatch, tmp_path, db_file) -> None:
    monkeypatch.setattr(mcp_mod, "FILINGS_13F_GLOB", str(tmp_path / "13f" / "**" / "*.parquet"))
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))
    assert mcp_mod.get_13f_holdings("037833100") == []


def test_real_connect_path(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "real.duckdb"
    init_db(db_path)
    monkeypatch.setattr(mcp_mod, "DB_PATH", db_path)

    assert mcp_mod.get_filings("NOPE") == []
