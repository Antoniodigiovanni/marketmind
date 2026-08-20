"""Tests for the MCP tool implementations (no live calls, in-memory DB)."""

from __future__ import annotations

import datetime as dt
import uuid

import polars as pl
import pytest

import server.mcp as mcp_mod
from agent.ingestion import RawNewsArchive
from data.db.companies import create_company
from data.db.init import init_db
from data.db.listings import upsert_listing
from data.db.prices import replace_all_volatility, upsert_prices
from data.fetch import ensure_index_listing
from data.sources.rss import NEWS_RECORD_FIELDS, Article, article_to_record


async def _mcp_listing(pg_conn, symbol: str = "AAPL") -> uuid.UUID:
    company_id = await create_company(
        pg_conn, name="Apple", country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )
    return await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol=symbol,
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )


def _pool_returning(pg_conn):
    """Build a fake `_pg_pool` whose `.connection()` yields the test's own
    transaction-scoped connection, so tool calls run inside the same
    rolled-back transaction as the rest of the test."""

    class _FakeConnCtx:
        async def __aenter__(self):
            return pg_conn

        async def __aexit__(self, *exc):
            return False

    class _FakePool:
        def connection(self):
            return _FakeConnCtx()

    async def fake_pg_pool():
        return _FakePool()

    return fake_pg_pool


def _article(**overrides: str) -> Article:
    """Build an Article with sensible defaults for the news round-trip tests."""
    fields: dict[str, str] = {
        "ticker": "AAPL",
        "url": "https://a.com/1",
        "title": "Apple unveils new chip",
        "summary": "Apple announced a new M-series chip.",
        "source": "google-news",
        "published_at": "2026-08-04T08:00:00+00:00",
    }
    fields.update(overrides)
    return Article(**fields)


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


async def test_get_prices_returns_rows_for_resolved_symbol(pg_conn, monkeypatch) -> None:
    listing_id = await _mcp_listing(pg_conn)
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 1),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1000,
                "currency": "USD",
            },
            {
                "date": dt.date(2026, 8, 4),
                "open": 101.0,
                "high": 103.0,
                "low": 100.0,
                "close": 102.0,
                "volume": 1200,
                "currency": "USD",
            },
        ],
    )
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))

    rows = await mcp_mod.get_prices("AAPL", start="2026-08-01")
    assert len(rows) == 2
    assert rows[0]["close"] == 101.0
    assert rows[0]["date"] == "2026-08-01"


async def test_get_prices_unknown_ticker_returns_empty(pg_conn, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))
    assert await mcp_mod.get_prices("NOPE") == []


async def test_get_volatility_stock_reads_from_postgres(pg_conn, monkeypatch) -> None:
    listing_id = await _mcp_listing(pg_conn)
    await replace_all_volatility(
        pg_conn,
        rows=[
            {"listing_id": listing_id, "date": dt.date(2026, 8, 4), "rv_20d": 18.5, "rv_60d": 21.0}
        ],
    )
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))

    rows = await mcp_mod.get_volatility("AAPL")
    assert rows[0]["rv_20d"] == 18.5


async def test_get_volatility_index_reads_from_prices(pg_conn, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))
    listing_id = await ensure_index_listing(pg_conn, symbol="^VIX", name="CBOE Volatility Index")
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 4),
                "open": 14.0,
                "high": 14.5,
                "low": 13.5,
                "close": 14.2,
                "volume": 0,
                "currency": "USD",
            }
        ],
    )

    rows = await mcp_mod.get_volatility("^VIX")
    assert rows[0]["close"] == 14.2


def test_news_read_columns_exist_in_the_written_record() -> None:
    """Guard against producer/consumer drift on the raw news schema.

    The columns the MCP tools select must exist in the record the ingest job
    actually writes. Asserted against emitted keys, not just the constant.
    """
    record = article_to_record(_article())
    assert set(record) == set(NEWS_RECORD_FIELDS)
    assert set(mcp_mod.NEWS_COLUMNS) <= set(record)


def test_get_news_and_search(tmp_path, monkeypatch) -> None:
    """Round-trip: write via the ingest producer, read via the MCP tools."""
    RawNewsArchive(tmp_path / "2026-08-04.ndjson").append([_article()])
    monkeypatch.setattr(mcp_mod, "NEWS_GLOB", str(tmp_path / "*.ndjson"))

    news = mcp_mod.get_news("AAPL")
    assert len(news) == 1
    assert news[0]["title"] == "Apple unveils new chip"
    assert news[0]["summary"] == "Apple announced a new M-series chip."
    assert news[0]["source"] == "google-news"

    hits = mcp_mod.search_news("chip")
    assert len(hits) == 1
    assert mcp_mod.search_news("M-series") == hits  # matches on summary, not title
    assert mcp_mod.search_news("bonds") == []


def test_get_news_since_filter_and_empty_search(tmp_path, monkeypatch) -> None:
    RawNewsArchive(tmp_path / "2026-08-04.ndjson").append(
        [
            _article(title="Newer", url="https://a.com/1"),
            _article(
                title="Older", url="https://a.com/2", published_at="2026-08-01T08:00:00+00:00"
            ),
        ]
    )
    monkeypatch.setattr(mcp_mod, "NEWS_GLOB", str(tmp_path / "*.ndjson"))

    rows = mcp_mod.get_news("AAPL", since="2026-08-02")
    assert [r["title"] for r in rows] == ["Newer"]

    monkeypatch.setattr(mcp_mod, "NEWS_GLOB", str(tmp_path / "missing" / "*.ndjson"))
    assert mcp_mod.get_news("AAPL") == []
    assert mcp_mod.search_news("anything") == []


async def test_get_volatility_unresolved_ticker_returns_empty(pg_conn, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))
    assert await mcp_mod.get_volatility("NOPE") == []


async def test_get_volatility_date_window(pg_conn, monkeypatch) -> None:
    listing_id = await _mcp_listing(pg_conn)
    await replace_all_volatility(
        pg_conn,
        rows=[
            {"listing_id": listing_id, "date": dt.date(2026, 8, 3), "rv_20d": 17.0, "rv_60d": 20.0},
            {"listing_id": listing_id, "date": dt.date(2026, 8, 4), "rv_20d": 18.5, "rv_60d": 21.0},
        ],
    )
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))

    rows = await mcp_mod.get_volatility("AAPL", start="2026-08-04", end="2026-08-04")
    assert [r["date"] for r in rows] == ["2026-08-04"]


def test_main_initialises_both_databases_then_serves_with_auth(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_apply_schema(dsn: str) -> None:
        calls.append(f"apply_schema:{dsn}")

    def fake_init_db() -> None:
        calls.append("init_db")

    def fake_run(app: object, *, host: str, port: int, log_level: str) -> None:
        calls.append(f"uvicorn.run:{host}:{port}")

    monkeypatch.setattr(mcp_mod, "apply_schema", fake_apply_schema)
    monkeypatch.setattr(mcp_mod, "init_db", fake_init_db)
    monkeypatch.setattr(mcp_mod.uvicorn, "run", fake_run)
    monkeypatch.setattr(mcp_mod, "dsn_from_env", lambda: "postgresql://x")
    monkeypatch.setenv("MARKETMIND_MCP_TOKEN", "secret-token")

    mcp_mod.main()

    assert calls == [
        "apply_schema:postgresql://x",
        "init_db",
        f"uvicorn.run:127.0.0.1:{mcp_mod.MCP_PORT}",
    ]


def test_main_requires_the_mcp_token(monkeypatch) -> None:
    async def fake_apply_schema(dsn: str) -> None:
        return None

    monkeypatch.setattr(mcp_mod, "apply_schema", fake_apply_schema)
    monkeypatch.setattr(mcp_mod, "init_db", lambda: None)
    monkeypatch.delenv("MARKETMIND_MCP_TOKEN", raising=False)

    with pytest.raises(KeyError, match="MARKETMIND_MCP_TOKEN"):
        mcp_mod.main()


class _FakeASGIApp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append((scope["type"],))


async def test_bearer_auth_middleware_rejects_missing_or_wrong_token() -> None:
    inner = _FakeASGIApp()
    middleware = mcp_mod._BearerAuthMiddleware(inner, token="correct")

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "headers": [(b"authorization", b"Bearer wrong")]}
    await middleware(scope, None, send)

    assert inner.calls == []  # never reached the wrapped app
    assert sent[0]["status"] == 401


async def test_bearer_auth_middleware_accepts_the_correct_token() -> None:
    inner = _FakeASGIApp()
    middleware = mcp_mod._BearerAuthMiddleware(inner, token="correct")

    scope = {"type": "http", "headers": [(b"authorization", b"Bearer correct")]}
    await middleware(scope, None, None)

    assert inner.calls == [("http",)]


async def test_bearer_auth_middleware_passes_through_non_http_scopes() -> None:
    """The lifespan scope (startup/shutdown) must never be auth-gated."""
    inner = _FakeASGIApp()
    middleware = mcp_mod._BearerAuthMiddleware(inner, token="correct")

    scope = {"type": "lifespan"}
    await middleware(scope, None, None)

    assert inner.calls == [("lifespan",)]


async def test_market_views_tools(monkeypatch, pg_conn) -> None:
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))

    view_id = await mcp_mod.save_market_view_tool(
        as_of="2026-08-04",
        view_text="Equities skew to the upside.",
        model="hermes",
        data_cutoff="2026-08-04",
    )
    views = await mcp_mod.get_market_views(as_of="2026-08-04")
    assert str(views[0]["view_id"]) == view_id


def test_connect_raises_when_db_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mcp_mod, "DB_PATH", tmp_path / "missing.duckdb")
    with pytest.raises(FileNotFoundError, match="not initialised"):
        mcp_mod._connect()


def test_get_macro_reads_parquet_with_filters(tmp_path, monkeypatch) -> None:
    macro_dir = tmp_path / "macro"
    macro_dir.mkdir()
    pl.DataFrame(
        {
            "date": [dt.date(2026, 8, 3), dt.date(2026, 8, 4)],
            "value": [4.11, 4.09],
        }
    ).write_parquet(macro_dir / "DGS10.parquet")
    monkeypatch.setattr(mcp_mod, "MACRO_DIR", macro_dir)

    rows = mcp_mod.get_macro("DGS10")

    assert len(rows) == 2
    assert rows[0]["series"] == "DGS10"
    assert rows[0]["name"] == "10-Year Treasury Constant Maturity Rate"
    assert rows[0]["unit"] == "percent"
    assert rows[0]["frequency"] == "daily"
    assert rows[0]["date"] == "2026-08-03"

    filtered = mcp_mod.get_macro("DGS10", start="2026-08-04", end="2026-08-04")
    assert len(filtered) == 1
    assert filtered[0]["value"] == 4.09


def test_get_macro_unknown_series_returns_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "MACRO_DIR", tmp_path / "macro")
    assert mcp_mod.get_macro("NOPE") == []


def test_get_macro_missing_file_returns_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "MACRO_DIR", tmp_path / "macro")
    assert mcp_mod.get_macro("DGS10") == []


async def test_save_market_view_tool_stores_used_fact_ids(monkeypatch, pg_conn) -> None:
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))

    fact_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    view_id = await mcp_mod.save_market_view_tool(
        as_of="2026-08-04",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff="2026-08-04",
        used_fact_ids=fact_ids,
    )
    views = await mcp_mod.get_market_views(as_of="2026-08-05")

    assert str(views[0]["view_id"]) == view_id
    assert sorted(str(f) for f in views[0]["used_fact_ids"]) == sorted(fact_ids)


def test_get_dividends_reads_parquet_with_filters(tmp_path, monkeypatch) -> None:
    dividends_dir = tmp_path / "dividends"
    dividends_dir.mkdir()
    pl.DataFrame(
        {
            "ex_date": [dt.date(2026, 2, 13), dt.date(2026, 5, 15)],
            "amount": [0.25, 0.26],
            "currency": ["USD", "USD"],
        }
    ).write_parquet(dividends_dir / "AAPL.parquet")
    monkeypatch.setattr(mcp_mod, "DIVIDENDS_DIR", dividends_dir)

    rows = mcp_mod.get_dividends("AAPL")

    assert len(rows) == 2
    assert rows[0]["ex_date"] == "2026-05-15"
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["amount"] == 0.26

    filtered = mcp_mod.get_dividends("AAPL", since="2026-05-01", as_of="2026-05-31")
    assert len(filtered) == 1
    assert filtered[0]["ex_date"] == "2026-05-15"


def test_get_dividends_missing_file_returns_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "DIVIDENDS_DIR", tmp_path / "dividends")
    assert mcp_mod.get_dividends("NOPE") == []


def test_get_short_interest_reads_partitions(tmp_path, monkeypatch) -> None:
    si_dir = tmp_path / "short_interest"
    si_dir.mkdir()
    pl.DataFrame(
        {
            "symbol": ["AAPL"],
            "issue_name": ["Apple Inc."],
            "market_class": ["NASDAQ"],
            "current_short": [50_000_000.0],
            "previous_short": [48_000_000.0],
            "avg_daily_volume": [60_000_000.0],
            "days_to_cover": [0.83],
            "change_pct": [4.2],
            "settlement_date": [dt.date(2026, 7, 15)],
        }
    ).write_parquet(si_dir / "short_interest_2026-07-15.parquet")
    monkeypatch.setattr(mcp_mod, "SHORT_INTEREST_GLOB", str(si_dir / "*.parquet"))

    rows = mcp_mod.get_short_interest("AAPL")

    assert len(rows) == 1
    assert rows[0]["settlement_date"] == "2026-07-15"
    assert rows[0]["current_short"] == 50_000_000.0
    assert mcp_mod.get_short_interest("AAPL", as_of="2026-07-01") == []


def test_get_short_interest_no_partitions_returns_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_mod, "SHORT_INTEREST_GLOB", str(tmp_path / "si" / "*.parquet"))
    assert mcp_mod.get_short_interest("AAPL") == []
