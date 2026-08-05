"""DuckDB write helpers for the filings pipeline."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from pathlib import Path

import duckdb
import polars as pl

from data.filings.parse.form13f import HOLDINGS_COLUMNS
from data.filings.parse.ownership import InsiderHolding, InsiderTransaction

COVERED_FORMS = {
    "10-K",
    "10-Q",
    "8-K",
    "20-F",
    "6-K",
    "3",
    "4",
    "5",
    "13F-HR",
    "13F-HR/A",
    "DEF 14A",
}


def sha256_hex(value: str) -> str:
    """Return the sha256 hex digest of a string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def filing_exists(conn: duckdb.DuckDBPyConnection, cik: str, accession: str) -> bool:
    """Return True if the filing is already stored."""
    row = conn.execute(
        "SELECT 1 FROM filings WHERE cik = ? AND accession_number = ?",
        [cik, accession],
    ).fetchone()
    return row is not None


def insert_filing(
    conn: duckdb.DuckDBPyConnection,
    *,
    cik: str,
    accession_number: str,
    form_type: str,
    filed_at: str,
    period_of_report: str | None,
    primary_document: str,
    url: str,
    text_path: str | None = None,
    text_hash: str | None = None,
    size_bytes: int | None = None,
    ticker: str | None = None,
) -> str:
    """Insert a filing metadata row and return its id."""
    filing_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO filings (id, accession_number, cik, ticker, form_type, filed_at,
                             period_of_report, primary_document, url, url_hash,
                             text_path, text_hash, size_bytes, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fetched')
        """,
        [
            filing_id,
            accession_number,
            cik,
            ticker,
            form_type,
            filed_at,
            period_of_report,
            primary_document,
            url,
            sha256_hex(url),
            text_path,
            text_hash,
            size_bytes,
        ],
    )
    return filing_id


def insert_filing_text(conn: duckdb.DuckDBPyConnection, filing_id: str, text: str) -> None:
    """Store normalized filing text for full-text search."""
    conn.execute("INSERT INTO filing_text (filing_id, text) VALUES (?, ?)", [filing_id, text])


def create_filing_article(
    conn: duckdb.DuckDBPyConnection,
    *,
    filing_id: str,
    ticker: str,
    form_type: str,
    filed_at: str,
    url: str,
    text_hash: str,
) -> str:
    """Queue a filing for fact extraction as an article row."""
    article_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO articles (id, url_hash, content_hash, url, source, ticker,
                              published_at, fetched_at, title, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, NOW(), ?, 'pending')
        """,
        [
            article_id,
            sha256_hex(url),
            text_hash,
            url,
            f"sec:{form_type}",
            ticker,
            f"{filed_at} 00:00:00+00",
            f"{ticker} {form_type} filed {filed_at}",
        ],
    )
    conn.execute(
        "UPDATE filings SET article_id = ?, status = 'facts_pending' WHERE id = ?",
        [article_id, filing_id],
    )
    return article_id


def insert_insider_rows(
    conn: duckdb.DuckDBPyConnection,
    filing_id: str,
    transactions: list[InsiderTransaction],
    holdings: list[InsiderHolding],
) -> tuple[int, int]:
    """Insert insider rows; returns (n_transactions, n_holdings)."""
    for tx in transactions:
        conn.execute(
            """
            INSERT INTO insider_transactions (id, filing_id, ticker, owner_name, owner_cik,
                owner_title, security_title, transaction_date, deemed_execution_date,
                transaction_code, shares_transacted, price_per_share, shares_held_after,
                direct_or_indirect)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                uuid.uuid4().hex,
                filing_id,
                tx.ticker,
                tx.owner_name,
                tx.owner_cik,
                tx.owner_title,
                tx.security_title,
                tx.transaction_date,
                tx.deemed_execution_date,
                tx.transaction_code,
                tx.shares_transacted,
                tx.price_per_share,
                tx.shares_held_after,
                tx.direct_or_indirect,
            ],
        )
    for holding in holdings:
        conn.execute(
            """
            INSERT INTO insider_holdings (id, filing_id, ticker, owner_name, owner_cik,
                owner_title, security_title, ref_date, shares_held_after,
                direct_or_indirect, form_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                uuid.uuid4().hex,
                filing_id,
                holding.ticker,
                holding.owner_name,
                holding.owner_cik,
                holding.owner_title,
                holding.security_title,
                holding.ref_date,
                holding.shares_held_after,
                holding.direct_or_indirect,
                holding.form_type,
            ],
        )
    return len(transactions), len(holdings)


def insert_13f_filing(
    conn: duckdb.DuckDBPyConnection,
    *,
    filing_id: str,
    filer_cik: str,
    filer_name: str | None,
    report_period: str,
    total_value: float | None,
) -> str:
    """Insert a 13F filing metadata row and return its id."""
    row_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO form13f_filings (id, filing_id, filer_cik, filer_name, report_period, total_value)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [row_id, filing_id, filer_cik, filer_name, report_period, total_value],
    )
    return row_id


def append_13f_holdings(df: pl.DataFrame, *, root: Path, report_period: str) -> Path:
    """Append parsed holdings to the quarterly Parquet partition."""
    year = report_period[:4]
    quarter = f"Q{(int(report_period[5:7]) - 1) // 3 + 1}"
    target = root / "13f" / year / f"{quarter}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = pl.read_parquet(target)
        df = pl.concat([existing, df], how="vertical_relaxed")
    df.select(HOLDINGS_COLUMNS).write_parquet(target)
    return target


def log_run(
    conn: duckdb.DuckDBPyConnection,
    *,
    job_type: str,
    run_date: str,
    started_at: dt.datetime,
    filings_fetched: int = 0,
    filings_parsed: int = 0,
    filings_skipped: int = 0,
    insider_rows: int = 0,
    form13f_rows: int = 0,
) -> None:
    """Write a filings job audit row to agent_runs."""
    conn.execute(
        """
        INSERT INTO agent_runs (id, run_date, job_type, started_at, finished_at,
                                filings_fetched, filings_parsed, filings_skipped,
                                insider_rows, form13f_rows)
        VALUES (?, ?, ?, ?, NOW(), ?, ?, ?, ?, ?)
        """,
        [
            uuid.uuid4().hex,
            run_date,
            job_type,
            started_at,
            filings_fetched,
            filings_parsed,
            filings_skipped,
            insider_rows,
            form13f_rows,
        ],
    )
