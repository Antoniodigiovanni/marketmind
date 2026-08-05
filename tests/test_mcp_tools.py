"""Tests for the MCP tool implementations (no live calls, in-memory DB)."""

from __future__ import annotations

import datetime as dt

import duckdb
import polars as pl
import pytest

import server.mcp as mcp_mod
from data.db.init import init_db


@pytest.fixture()
def db_file(tmp_path) -> str:
    """Shared file-backed DB so each _connect() call sees prior writes."""
    path = tmp_path / "mcp.duckdb"
    connection = init_db(path)
    connection.execute(
        """
        INSERT INTO articles (id, url_hash, content_hash, url, source, ticker,
                              published_at, fetched_at, status)
        VALUES ('art-1', 'h1', 'c1', 'https://example.com', 'reuters', 'AAPL',
                '2026-01-01 09:00:00+00', '2026-01-01 10:00:00+00', 'processed')
        """
    )
    connection.close()
    return str(path)


def test_get_prices_reads_parquet(tmp_path, monkeypatch) -> None:
    prices_dir = tmp_path / "prices"
    prices_dir.mkdir()
    pl.DataFrame(
        {
            "date": [dt.date(2026, 8, 1), dt.date(2026, 8, 4)],
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000, 1200],
        }
    ).write_parquet(prices_dir / "AAPL.parquet")
    monkeypatch.setattr(mcp_mod, "PRICES_DIR", prices_dir)

    rows = mcp_mod.get_prices("AAPL", start="2026-08-01")
    assert len(rows) == 2
    assert rows[0]["close"] == 101.0
    assert rows[0]["date"] == "2026-08-01"


def test_get_prices_unknown_ticker_returns_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "PRICES_DIR", tmp_path / "prices")
    assert mcp_mod.get_prices("NOPE") == []


def test_get_volatility_stock_reads_processed(tmp_path, monkeypatch) -> None:
    vol_path = tmp_path / "volatility.parquet"
    pl.DataFrame(
        {
            "date": [dt.date(2026, 8, 4)],
            "ticker": ["AAPL"],
            "rv_20d": [18.5],
            "rv_60d": [21.0],
        }
    ).write_parquet(vol_path)
    monkeypatch.setattr(mcp_mod, "PROCESSED_VOL_PATH", vol_path)

    rows = mcp_mod.get_volatility("AAPL")
    assert rows[0]["rv_20d"] == 18.5


def test_get_volatility_index_uses_prices(tmp_path, monkeypatch) -> None:
    macro_dir = tmp_path / "macro"
    macro_dir.mkdir()
    pl.DataFrame(
        {
            "date": [dt.date(2026, 8, 4)],
            "open": [14.0],
            "high": [14.5],
            "low": [13.5],
            "close": [14.2],
            "volume": [0],
        }
    ).write_parquet(macro_dir / "^VIX.parquet")
    monkeypatch.setattr(mcp_mod, "MACRO_DIR", macro_dir)
    monkeypatch.setattr(mcp_mod, "PRICES_DIR", macro_dir)

    rows = mcp_mod.get_volatility("^VIX")
    assert rows[0]["close"] == 14.2


def test_get_news_and_search(tmp_path, monkeypatch) -> None:
    news_file = tmp_path / "news.ndjson"
    news_file.write_text(
        '{"url": "https://a.com/1", "source": "google", "ticker": "AAPL", '
        '"published_at": "2026-08-04 08:00:00+00", "title": "Apple unveils new chip", '
        '"description": "Apple announced a new M-series chip.", "content": "..."}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_mod, "NEWS_GLOB", str(news_file))

    news = mcp_mod.get_news("AAPL")
    assert len(news) == 1
    assert news[0]["title"] == "Apple unveils new chip"

    hits = mcp_mod.search_news("chip")
    assert len(hits) == 1
    assert mcp_mod.search_news("bonds") == []


def test_get_news_since_filter_and_empty_search(tmp_path, monkeypatch) -> None:
    news_file = tmp_path / "news.ndjson"
    news_file.write_text(
        '{"url": "https://a.com/1", "source": "google", "ticker": "AAPL", '
        '"published_at": "2026-08-04 08:00:00+00", "title": "Newer", '
        '"description": "desc", "content": "..."}\n'
        '{"url": "https://a.com/2", "source": "google", "ticker": "AAPL", '
        '"published_at": "2026-08-01 08:00:00+00", "title": "Older", '
        '"description": "desc", "content": "..."}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_mod, "NEWS_GLOB", str(news_file))

    rows = mcp_mod.get_news("AAPL", since="2026-08-02")
    assert [r["title"] for r in rows] == ["Newer"]

    monkeypatch.setattr(mcp_mod, "NEWS_GLOB", str(tmp_path / "missing" / "*.ndjson"))
    assert mcp_mod.get_news("AAPL") == []
    assert mcp_mod.search_news("anything") == []


def test_get_volatility_missing_file_and_date_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "PROCESSED_VOL_PATH", tmp_path / "missing.parquet")
    assert mcp_mod.get_volatility("AAPL") == []

    vol_path = tmp_path / "volatility.parquet"
    pl.DataFrame(
        {
            "date": [dt.date(2026, 8, 3), dt.date(2026, 8, 4)],
            "ticker": ["AAPL", "AAPL"],
            "rv_20d": [17.0, 18.5],
            "rv_60d": [20.0, 21.0],
        }
    ).write_parquet(vol_path)
    monkeypatch.setattr(mcp_mod, "PROCESSED_VOL_PATH", vol_path)

    rows = mcp_mod.get_volatility("AAPL", start="2026-08-04", end="2026-08-04")
    assert [r["date"] for r in rows] == ["2026-08-04"]


def test_main_runs_server(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mcp_mod.mcp, "run", lambda: calls.append("run"))
    mcp_mod.main()
    assert calls == ["run"]


def test_facts_tool_point_in_time(monkeypatch, db_file) -> None:
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))
    mcp_mod.save_fact(
        ticker="AAPL",
        statement="AAPL guided higher.",
        fact_type="guidance",
        confidence=0.8,
        valid_from="2026-01-02",
        source_article_id="art-1",
    )
    facts = mcp_mod.get_facts("AAPL", "2026-01-03")
    assert facts[0]["statement"] == "AAPL guided higher."


def test_market_views_tools(monkeypatch, db_file) -> None:
    monkeypatch.setattr(mcp_mod, "_connect", lambda: duckdb.connect(db_file))
    view_id = mcp_mod.save_market_view_tool(
        as_of="2026-08-04",
        view_text="Equities skew to the upside.",
        model="hermes",
    )
    views = mcp_mod.get_market_views(as_of="2026-08-04")
    assert views[0]["id"] == view_id


def test_run_ingest_tool(monkeypatch) -> None:
    calls: dict[str, str] = {}

    def fake_run(date: str) -> None:
        calls["date"] = date

    monkeypatch.setattr("agent.ingestion.run_daily_ingest", fake_run)
    result = mcp_mod.run_ingest("2026-08-04")
    assert result == "Ingest completed for 2026-08-04"
    assert calls == {"date": "2026-08-04"}


def test_connect_raises_when_db_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mcp_mod, "DB_PATH", tmp_path / "missing.duckdb")
    with pytest.raises(FileNotFoundError, match="not initialised"):
        mcp_mod._connect()
