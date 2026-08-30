"""Tests for database initialisation from schema.sql."""

from __future__ import annotations

from pathlib import Path

from data.db import init as db_init
from data.db.init import init_db


def test_default_db_path_accepts_a_runtime_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MARKETMIND_DUCKDB_PATH", str(tmp_path / "runtime.duckdb"))

    assert db_init._default_db_path() == tmp_path / "runtime.duckdb"


EXPECTED_TABLES = {
    "agent_runs",
    "aggregate_facts",
    "articles",
    "companies",
    "company_profiles",
    "esef_filings",
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
            "profiles_built",
            "profiles_failed",
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


def test_esef_filings_schema_columns_exist() -> None:
    conn = init_db(":memory:")
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info('esef_filings')").fetchall()}
        assert {
            "fxo_id",
            "lei",
            "ticker",
            "entity_name",
            "country",
            "filing_system",
            "provider",
            "filed_at",
            "period_end",
            "package_url",
            "xhtml_url",
            "json_url",
            "text_path",
            "text_hash",
            "size_bytes",
            "status",
        } <= cols
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


def test_market_views_has_used_fact_ids_column() -> None:
    conn = init_db(":memory:")
    try:
        view_cols = {row[1] for row in conn.execute("PRAGMA table_info('market_views')").fetchall()}
        assert "used_fact_ids" in view_cols
    finally:
        conn.close()


def test_companies_and_aggregate_facts_schema() -> None:
    conn = init_db(":memory:")
    try:
        company_cols = {row[1] for row in conn.execute("PRAGMA table_info('companies')").fetchall()}
        assert {
            "ticker",
            "name",
            "country",
            "sector",
            "description",
            "description_source",
        } <= company_cols

        agg_cols = {
            row[1] for row in conn.execute("PRAGMA table_info('aggregate_facts')").fetchall()
        }
        assert {
            "id",
            "dimension",
            "dimension_key",
            "statement",
            "valid_from",
            "valid_until",
            "evidence",
            "embedding_id",
        } <= agg_cols
    finally:
        conn.close()


def test_company_profiles_schema() -> None:
    conn = init_db(":memory:")
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info('company_profiles')").fetchall()}
        assert {
            "id",
            "ticker",
            "description",
            "products_services",
            "website_url",
            "sources",
            "generated_by",
            "valid_from",
            "valid_until",
        } <= cols
    finally:
        conn.close()
