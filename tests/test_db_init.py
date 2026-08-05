"""Tests for database initialisation from schema.sql."""

from __future__ import annotations

from data.db.init import init_db

EXPECTED_TABLES = {
    "agent_runs",
    "articles",
    "fetch_checkpoints",
    "filing_text",
    "filings",
    "form13f_filings",
    "insider_holdings",
    "insider_transactions",
    "market_view_structures",
    "market_views",
    "ticker_facts",
}


def test_filings_schema_columns_exist() -> None:
    conn = init_db(":memory:")
    try:
        run_cols = {row[1] for row in conn.execute("PRAGMA table_info('agent_runs')").fetchall()}
        assert {
            "job_type",
            "filings_fetched",
            "filings_parsed",
            "filings_skipped",
            "insider_rows",
            "form13f_rows",
        } <= run_cols

        filing_cols = {row[1] for row in conn.execute("PRAGMA table_info('filings')").fetchall()}
        assert {
            "accession_number",
            "cik",
            "form_type",
            "filed_at",
            "period_of_report",
            "article_id",
        } <= filing_cols
    finally:
        conn.close()


def test_init_db_creates_all_tables() -> None:
    conn = init_db(":memory:")
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        assert EXPECTED_TABLES.issubset(tables)
    finally:
        conn.close()


def test_init_db_is_idempotent_and_preserves_data(tmp_path) -> None:
    db_path = tmp_path / "test.duckdb"

    conn1 = init_db(db_path)
    conn1.execute(
        "INSERT INTO agent_runs (id, run_date, started_at) VALUES ('run-1', '2026-01-01', NOW())"
    )
    conn1.close()

    conn2 = init_db(db_path)
    try:
        count = conn2.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        assert count == 1
    finally:
        conn2.close()
