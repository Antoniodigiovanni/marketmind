"""PostgreSQL-backed MCP tool tests."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

import server.mcp as mcp_mod
from data.db.articles import register_article
from data.db.identifiers import get_or_create_identifier
from data.db.listings import upsert_listing
from data.db.market_data import upsert_dividends, upsert_macro_series, upsert_short_interest
from data.db.prices import replace_all_volatility, upsert_prices
from data.fetch import ensure_index_listing


def _pool_returning(pg_conn):
    class _ConnectionContext:
        async def __aenter__(self):
            return pg_conn

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _Pool:
        def connection(self):
            return _ConnectionContext()

    async def fake_pool():
        return _Pool()

    return fake_pool


async def _company_listing(pg_conn, ticker: str = "AAPL") -> tuple[uuid.UUID, uuid.UUID]:
    company_id = await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value=ticker,
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name=ticker,
        country="US",
        sector="Technology",
    )
    listing_id = await upsert_listing(
        pg_conn,
        company_id=company_id,
        provider="yahoo",
        symbol=ticker,
        status="valid",
        valid_from=dt.date(2000, 1, 1),
    )
    return company_id, listing_id


async def test_price_and_volatility_tools(pg_conn, monkeypatch) -> None:
    _, listing_id = await _company_listing(pg_conn)
    await upsert_prices(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "date": dt.date(2026, 8, 4),
                "open": 100.0,
                "high": 103.0,
                "low": 99.0,
                "close": 102.0,
                "volume": 1200,
                "currency": "USD",
            }
        ],
    )
    await replace_all_volatility(
        pg_conn,
        rows=[
            {
                "listing_id": listing_id,
                "date": dt.date(2026, 8, 4),
                "rv_20d": 18.5,
                "rv_60d": 21.0,
            }
        ],
    )
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))
    assert (await mcp_mod.get_prices("AAPL"))[0]["close"] == 102.0
    assert (await mcp_mod.get_volatility("AAPL"))[0]["rv_20d"] == 18.5
    assert await mcp_mod.get_prices("NOPE") == []


async def test_volatility_index_tool(pg_conn, monkeypatch) -> None:
    listing_id = await ensure_index_listing(pg_conn, symbol="^VIX", name="VIX")
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
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))
    assert (await mcp_mod.get_volatility("^VIX"))[0]["close"] == 14.2


async def test_news_tools_read_postgres(pg_conn, monkeypatch) -> None:
    company_id, _ = await _company_listing(pg_conn)
    await register_article(
        pg_conn,
        company_id=company_id,
        url_hash="url-1",
        content_hash="content-1",
        url="https://example.com/apple",
        source="google-news",
        title="Apple unveils new chip",
        summary="A new M-series chip was announced.",
        published_at=dt.datetime(2026, 8, 4, 8, tzinfo=dt.UTC),
        pre_score=0.8,
    )
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))
    rows = await mcp_mod.get_news("AAPL", since="2026-08-04")
    assert rows[0]["summary"] == "A new M-series chip was announced."
    assert (await mcp_mod.search_news("M-series"))[0]["ticker"] == "AAPL"
    assert await mcp_mod.get_news("NOPE") == []


async def test_macro_dividend_and_short_interest_tools(pg_conn, monkeypatch) -> None:
    _, listing_id = await _company_listing(pg_conn)
    await upsert_macro_series(
        pg_conn,
        series_id="DGS10",
        source="fred",
        title="10-Year Treasury",
        unit="percent",
        frequency="daily",
        rows=[{"date": dt.date(2026, 8, 4), "value": 4.09}],
    )
    await upsert_dividends(
        pg_conn,
        listing_id=listing_id,
        rows=[{"ex_date": dt.date(2026, 5, 15), "amount": 0.26, "currency": "USD"}],
    )
    await upsert_short_interest(
        pg_conn,
        listing_id=listing_id,
        rows=[
            {
                "settlement_date": dt.date(2026, 7, 15),
                "current_short": 50_000_000,
                "previous_short": 48_000_000,
                "avg_daily_volume": 60_000_000,
                "days_to_cover": 0.83,
                "issue_name": "Apple Inc.",
                "market_class": "NASDAQ",
                "change_pct": 4.2,
            }
        ],
    )
    monkeypatch.setattr(mcp_mod, "_pg_pool", _pool_returning(pg_conn))
    assert (await mcp_mod.get_macro("DGS10"))[0]["value"] == 4.09
    assert (await mcp_mod.get_dividends("AAPL"))[0]["amount"] == 0.26
    assert (await mcp_mod.get_short_interest("AAPL"))[0]["current_short"] == 50_000_000


def test_main_applies_postgres_schema_only(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_apply_schema(dsn: str) -> None:
        calls.append(f"schema:{dsn}")

    monkeypatch.setattr(mcp_mod, "apply_schema", fake_apply_schema)
    monkeypatch.setattr(mcp_mod, "dsn_from_env", lambda: "postgresql://test")
    monkeypatch.setattr(
        mcp_mod.uvicorn,
        "run",
        lambda app, **kwargs: calls.append(f"serve:{kwargs['host']}:{kwargs['port']}"),
    )
    monkeypatch.setenv("MARKETMIND_MCP_TOKEN", "secret")
    mcp_mod.main()
    assert calls == ["schema:postgresql://test", f"serve:0.0.0.0:{mcp_mod.MCP_PORT}"]


@pytest.mark.parametrize("token", [None, "", "   "])
def test_main_requires_token(monkeypatch, token: str | None) -> None:
    async def fake_apply_schema(dsn: str) -> None:
        return None

    monkeypatch.setattr(mcp_mod, "apply_schema", fake_apply_schema)
    if token is None:
        monkeypatch.delenv("MARKETMIND_MCP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MARKETMIND_MCP_TOKEN", token)
    with pytest.raises(RuntimeError, match="MARKETMIND_MCP_TOKEN"):
        mcp_mod.main()


class _FakeApp:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True


async def test_bearer_auth_middleware() -> None:
    app = _FakeApp()
    middleware = mcp_mod._BearerAuthMiddleware(app, "correct")
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await middleware({"type": "http", "headers": [(b"authorization", b"Bearer wrong")]}, None, send)
    assert sent[0]["status"] == 401
    assert not app.called
