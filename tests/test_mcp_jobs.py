"""Tests for the MCP job-control tools (background runs, no live calls)."""

from __future__ import annotations

import datetime as dt
import json
import threading

import polars as pl
import pytest

import server.jobs as jobs
import server.mcp as mcp_mod
from agent.knowledge import get_current_facts
from data.db.init import init_db
from data.sources.rss import Article

DATE = "2026-08-04"

# Captured before the autouse offline fixture stubs it out.
_REAL_BUILD_VECTOR_STORE = mcp_mod._build_vector_store

UNIVERSE = pl.DataFrame(
    {
        "ticker": ["AAPL", "MSFT"],
        "name": ["Apple Inc", "Microsoft Corp"],
        "country": ["US", "US"],
        "sector": ["Technology", "Technology"],
        "added_date": [dt.date(2026, 1, 1), dt.date(2026, 1, 1)],
        "removed_date": [None, None],
        "universe": ["sp500", "sp500"],
    }
)


@pytest.fixture(autouse=True)
def _clear_slot(monkeypatch) -> None:
    """Give every test an empty run slot."""
    monkeypatch.setattr(jobs, "_CURRENT", None)


@pytest.fixture(autouse=True)
def _offline(monkeypatch) -> None:
    """Nothing in this module may reach the network."""
    monkeypatch.setattr(mcp_mod, "get_universe", lambda date: UNIVERSE)
    monkeypatch.setattr(mcp_mod, "_build_vector_store", lambda: None)
    monkeypatch.setenv("AI_API_KEY", "test-key")
    # The profile builder reaches Wikipedia (and from there the company site)
    # unless its fetchers are stubbed. Stub the network, keep the real builder.
    monkeypatch.setattr(
        "agent.company_profile.fetch_wikipedia_context",
        lambda name: {"extract": f"{name} designs things.", "page_url": None},
    )


def _fake_llm(messages: list[dict[str, str]]) -> str:
    """Answer the profile prompt and the extraction prompt with canned JSON."""
    user = messages[-1]["content"]
    if "Company Profile Generation" in user:
        return json.dumps(
            {
                "description": "Designs consumer electronics.",
                "products_services": ["hardware"],
                "website_url": None,
                "sources": {"wikipedia": None, "crawled": []},
            }
        )
    if "aggregate" in user:
        return json.dumps(
            [
                {
                    "statement": "Technology margins compress.",
                    "fact_type": "margin",
                    "confidence": 0.6,
                }
            ]
        )
    if "Apple raises Q3 guidance" in user:
        return json.dumps(
            [
                {
                    "statement": "Apple guided Q3 revenue above consensus.",
                    "fact_type": "guidance",
                    "confidence": 0.8,
                }
            ]
        )
    return "REDUNDANT"


def _fake_news(ticker: str, name: str) -> list[Article]:
    if ticker != "AAPL":
        return []
    return [
        Article(
            ticker="AAPL",
            url="https://example.com/aapl-1",
            title="Apple raises Q3 guidance",
            summary="Revenue above consensus.",
            source="google-news",
            published_at="2026-08-04T09:00:00+00:00",
        )
    ]


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """Point the tools at a temp DB with the model and network calls faked."""
    db_path = tmp_path / "research.duckdb"
    init_db(db_path).close()
    monkeypatch.setattr(mcp_mod, "DB_PATH", db_path)
    monkeypatch.setattr(mcp_mod, "_build_llm", lambda: _fake_llm)
    monkeypatch.setattr("agent.ingestion.fetch_company_news", _fake_news)
    monkeypatch.setattr("agent.ingestion.fetch_company_description", lambda name: "")
    monkeypatch.setattr("agent.ingestion.RAW_NEWS_DIR", tmp_path / "news")
    return db_path


def _join_run(timeout: float = 20.0) -> jobs.RunRecord:
    record = jobs.current_run()
    assert record is not None, "no run was started"
    record.thread.join(timeout=timeout)
    assert not record.thread.is_alive(), "run did not finish in time"
    return record


# --- synchronous refusals: no run is started, the caller gets a diagnostic ---


def test_run_ingest_rejects_a_malformed_date() -> None:
    result = mcp_mod.run_ingest("04-08-2026")

    assert "Invalid date" in result
    assert jobs.current_run() is None


def test_run_ingest_rejects_tickers_outside_the_universe() -> None:
    result = mcp_mod.run_ingest(DATE, tickers=["ZZZZ"])

    assert "ZZZZ" in result
    assert "No universe entries" in result
    assert jobs.current_run() is None


def test_run_ingest_reports_a_missing_api_key_instead_of_starting(monkeypatch) -> None:
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    result = mcp_mod.run_ingest(DATE)

    assert "Cannot start ingest" in result
    assert "API key" in result
    assert jobs.current_run() is None


def test_run_ingest_refuses_a_second_concurrent_run(wired) -> None:
    release = threading.Event()
    first_id = jobs.start_run("news", lambda progress: release.wait(timeout=5) and {})
    try:
        result = mcp_mod.run_ingest(DATE)
        assert first_id in result
        assert "refusing" in result
    finally:
        release.set()
        _join_run()


# --- the real pipeline, end to end, through the tool ---


def test_run_ingest_drives_the_real_pipeline(wired) -> None:
    result = mcp_mod.run_ingest(DATE)
    record = _join_run()

    assert "started" in result.lower()
    assert record.run_id in result
    # The original bug: run_daily_ingest was called without llm=, so every
    # invocation died on ValueError("llm callable is required").
    assert record.error is None
    assert record.state == "succeeded"
    assert record.counts["facts_written"] == 1
    assert record.tickers_done == 2
    assert record.tickers_total == 2

    conn = init_db(wired)
    try:
        facts = get_current_facts("AAPL", DATE, conn)
        assert len(facts) == 1
        assert "consensus" in facts[0]["statement"]
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_run_ingest_honours_a_ticker_subset(wired) -> None:
    mcp_mod.run_ingest(DATE, tickers=["aapl"])
    record = _join_run()

    assert record.state == "succeeded"
    assert record.tickers_total == 1


def test_run_ingest_names_unknown_tickers_but_runs_the_known_ones(wired) -> None:
    result = mcp_mod.run_ingest(DATE, tickers=["AAPL", "ZZZZ"])
    record = _join_run()

    assert "ZZZZ" in result
    assert record.state == "succeeded"
    assert record.tickers_total == 1


def test_run_ingest_passes_profiles_only_through(wired) -> None:
    mcp_mod.run_ingest(DATE, profiles_only=True)
    record = _join_run()

    assert record.state == "succeeded"
    assert record.counts["articles_fetched"] == 0
    assert record.counts["profiles_built"] == 2


def test_an_aborted_run_is_reported_by_the_status_tool(wired, monkeypatch) -> None:
    def exploding_llm(messages: list[dict[str, str]]) -> str:
        raise RuntimeError("provider is down")

    monkeypatch.setattr(mcp_mod, "_build_llm", lambda: exploding_llm)
    monkeypatch.setattr("agent.ingestion.MAX_CONSECUTIVE_FAILURES", 1)

    mcp_mod.run_ingest(DATE)
    record = _join_run()

    assert record.state == "failed"
    assert "consecutive" in record.error

    status = mcp_mod.get_ingest_status()
    assert status["run"]["state"] == "failed"
    assert "consecutive" in status["run"]["error"]


# --- status ---


def test_get_ingest_status_reports_history_and_no_live_run(wired) -> None:
    status = mcp_mod.get_ingest_status()

    assert status["run"] is None
    assert status["history"] == []


def test_get_ingest_status_reports_a_finished_run_and_its_audit_row(wired) -> None:
    mcp_mod.run_ingest(DATE)
    record = _join_run()

    status = mcp_mod.get_ingest_status()

    assert status["run"]["run_id"] == record.run_id
    assert status["run"]["state"] == "succeeded"
    assert status["run"]["tickers_done"] == 2
    assert status["run"]["elapsed_seconds"] >= 0.0
    assert len(status["history"]) == 1
    assert status["history"][0]["job_type"] == "news"
    assert status["history"][0]["facts_written"] == 1


def test_get_ingest_status_limits_history(wired) -> None:
    conn = init_db(wired)
    try:
        for i in range(3):
            conn.execute(
                """
                INSERT INTO agent_runs (id, run_date, job_type, started_at, finished_at)
                VALUES (?, ?, 'news', ?, NOW())
                """,
                [f"r{i}", dt.date(2026, 8, i + 1), f"2026-08-0{i + 1} 09:00:00+00"],
            )
    finally:
        conn.close()

    status = mcp_mod.get_ingest_status(limit=2)

    assert len(status["history"]) == 2
    assert status["history"][0]["id"] == "r2"


def test_status_is_readable_while_a_run_holds_the_database(wired) -> None:
    """The status tool opens its own connection while a run is mid-write."""
    writing = threading.Event()
    release = threading.Event()

    def slow_work(progress: jobs.ProgressCallback) -> dict[str, int]:
        conn = init_db(mcp_mod.DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO agent_runs (id, run_date, job_type, started_at)
                VALUES ('live', DATE '2026-08-04', 'news', NOW())
                """
            )
            progress(1, 2, {"facts_written": 0})
            writing.set()
            release.wait(timeout=5)
            return {"facts_written": 0}
        finally:
            conn.close()

    jobs.start_run("news", slow_work)
    try:
        assert writing.wait(timeout=5)
        status = mcp_mod.get_ingest_status()

        assert status["run"]["state"] == "running"
        assert status["run"]["tickers_done"] == 1
        assert status["run"]["finished_at"] is None
        assert [row["id"] for row in status["history"]] == ["live"]
    finally:
        release.set()
        _join_run()


# --- filings ---


class _EmptyEdgarClient:
    """EDGAR client that knows the CIK map but has no new submissions."""

    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        return {"AAPL": {"cik": "0000320193", "name": "Apple Inc."}}

    def submissions(self, cik: str) -> list[object]:
        return []


def test_run_filings_refresh_drives_the_real_fetch(wired, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mcp_mod, "_build_edgar_client", _EmptyEdgarClient)
    monkeypatch.setattr(mcp_mod, "ROOT_DIR", tmp_path)

    result = mcp_mod.run_filings_refresh(DATE)
    record = _join_run()

    assert record.run_id in result
    assert record.error is None
    assert record.state == "succeeded"
    assert record.counts["filings_fetched"] == 0

    conn = init_db(wired)
    try:
        row = conn.execute("SELECT job_type FROM agent_runs").fetchone()
        assert row == ("filings",)
    finally:
        conn.close()


def test_run_filings_refresh_defaults_to_today(wired, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mcp_mod, "_build_edgar_client", _EmptyEdgarClient)
    monkeypatch.setattr(mcp_mod, "ROOT_DIR", tmp_path)

    result = mcp_mod.run_filings_refresh()
    _join_run()

    assert dt.date.today().isoformat() in result


def test_run_filings_refresh_rejects_a_malformed_date() -> None:
    result = mcp_mod.run_filings_refresh("not-a-date")

    assert "Invalid date" in result
    assert jobs.current_run() is None


def test_run_filings_refresh_reports_an_empty_universe(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "get_universe", lambda date: UNIVERSE.clear())

    result = mcp_mod.run_filings_refresh(DATE)

    assert "No universe entries" in result
    assert jobs.current_run() is None


def test_run_filings_refresh_refuses_a_second_concurrent_run() -> None:
    release = threading.Event()
    first_id = jobs.start_run("news", lambda progress: release.wait(timeout=5) and {})
    try:
        result = mcp_mod.run_filings_refresh(DATE)
        assert first_id in result
        assert "refusing" in result
    finally:
        release.set()
        _join_run()


def test_run_filings_refresh_reports_a_client_it_cannot_build(monkeypatch) -> None:
    def broken_client() -> object:
        raise RuntimeError("no EDGAR user agent configured")

    monkeypatch.setattr(mcp_mod, "_build_edgar_client", broken_client)

    result = mcp_mod.run_filings_refresh(DATE)

    assert "Cannot start filings refresh" in result
    assert "EDGAR" in result
    assert jobs.current_run() is None


# --- the construction seams the tools rely on ---


def test_build_edgar_client_returns_a_usable_client() -> None:
    from data.sources.sec import EdgardClient

    assert isinstance(mcp_mod._build_edgar_client(), EdgardClient)


def test_build_vector_store_returns_a_fact_store(monkeypatch, tmp_path) -> None:
    from agent.embeddings import ChromaVectorStore

    monkeypatch.setattr("agent.embeddings.DEFAULT_VECTOR_DIR", tmp_path / "vectors")

    assert isinstance(_REAL_BUILD_VECTOR_STORE(), ChromaVectorStore)
