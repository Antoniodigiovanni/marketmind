# marketmind — Project Roadmap

## Mission

marketmind is the **market-memory microservice** for an agent harness (Hermes).
It owns a growing database of market data — daily closing prices, news, extracted
facts, and historical market views — and exposes that database through **MCP**.
The LLM harness is both a consumer and a writer: it reads prices/news/facts
through MCP tools, and writes free-text market views back into the database,
which a job then turns into structured data.

marketmind is **not a standalone app**. No UI, no separate deployment stack, no
own notification system. It runs as a service on the same box as the harness
(the Hermes Hetzner machine), shares its cron schedule and secrets, and speaks
only through MCP. Backtesting and strategy generation are **Phase 2** — parked
until the market memory is solid.

## Architecture

```
Hermes / my.life (agent harness)
        │  MCP (stdio locally, HTTP from the Hermes box)
        ▼
   marketmind (Python service)
   ├── daily jobs      prices · macro · news · fact extraction · view structuring
   ├── MCP server      read/write tools over the database
   └── storage         DuckDB (facts, views, audit) · Parquet (raw) · ChromaDB (vectors)
```

Everything the harness can touch goes through MCP tools. The database schema is
the single source of truth (`data/db/schema.sql`); raw downloads are immutable
Parquet/NDJSON under `data/raw/`.

## Repository layout (target)

```
marketmind/
├── data/
│   ├── raw/            ← immutable: prices/*.parquet, macro/*.parquet, news/{date}.ndjson
│   ├── processed/      ← calendar-aligned derived series (Phase 2 returns)
│   ├── universe/       ← point-in-time constituents: sp500.csv, eurostoxx600.csv, delisted.csv
│   ├── db/
│   │   ├── schema.sql  ← DuckDB schema (source of truth)
│   │   └── init.py     ← creates research.duckdb from schema.sql
│   └── sources/        ← yfinance.py, fred.py, ecb.py, rss.py, sec.py
├── agent/
│   ├── ingestion.py    ← daily news → fact pipeline
│   ├── views.py        ← market view write + structured extraction job
│   ├── knowledge.py    ← fact store read/write (as_of-enforced)
│   └── prompts/        ← all prompt templates as .md files
├── server/
│   └── mcp.py          ← MCP server: all harness-facing tools
├── factors/            ← Phase 2 (parked)
├── research/           ← Phase 2 (parked)
└── portfolio/          ← Phase 2 (parked)
```

## Branching & commit strategy

Apply once the repository is initialised (`git init` — pending).

- `main` is always deployable; `dev` is the integration branch; `feature/*` and
  `fix/*` branch from `dev`. No direct pushes to `main`/`dev`.
- Conventional Commits: `feat`, `fix`, `chore`, `test`, `docs`, `refactor`, `perf`
  with a module scope (e.g. `feat(ingest): add RSS fetch for Google News`).
- Every PR passes `ruff check`, `ruff format --check`, and `pytest`; business
  logic ships with tests in the same PR.

## Phase 1 — Market memory microservice (current)

Goal: the database grows daily without intervention, the harness can query and
write through MCP, and free-text views are structured automatically.

### 1.1 Database foundation
- [ ] `schema.sql`: `articles`, `ticker_facts`, `agent_runs`, `market_views`,
      `market_view_structures`
- [ ] `data/db/init.py` — reads `schema.sql`, creates `research.duckdb` idempotently
- [ ] CLI: `uv run python -m data.db.init`
- [ ] Test: schema creation is idempotent; in-memory DuckDB in tests

### 1.2 Universe
- [x] `data/universe/sp500.csv` and `eurostoxx600.csv` —
      columns: `ticker, name, country, sector, added_date, removed_date`
- [x] `data/universe/loader.py::get_universe(as_of)` — filters on
      `added_date <= as_of AND (removed_date IS NULL OR removed_date > as_of)`
- [ ] Delisted tickers logged in `data/universe/delisted.csv` and included in
      historical price fetches (survivorship compensation)
- [x] Test: point-in-time filtering with known add/remove dates
- [x] Automated refresh — `data/universe/update.py` pulls the official STOXX
      Europe 600 selection list (stoxx.com, keyless CSV, 600 members via
      rank <= 600) and S&P 500 holdings from iShares CSPX via the BlackRock
      fund-document API; applies append-only changes and refuses removals if a
      source drops >5% of members
- [x] `symbol_map` table + resolution job — members resolved by ISIN, then CUSIP,
      then ticker via Yahoo search (`v1/finance/search` accepts ISINs and
      CUSIPs — verified), validated against the chart API, with a review queue
      for unresolved symbols (`uv run python -m data.universe.resolve`)
- [ ] ISIN/CUSIP enrichment — SEC EDGAR N-PORT holdings for US CUSIPs
      (quarterly, official) and the iShares STOXX 600 UCITS fund document for EU
      ISINs; OpenFIGI does not return ISINs, so it is not used for enrichment
- [ ] Daily cron runs the refresh before the price fetch so new members enter
      scope the same day

### 1.3 Price + macro fetch (daily)
- [ ] `data/sources/yfinance.py::fetch_ohlcv` — adjusted OHLCV, EU tickers via
      exchange suffixes (`ASML.AS`, `SAP.DE`), UTC timestamps
- [ ] Exchange calendars (`exchange_calendars`) for US/EU sessions — each day
      fetches the correct last close per exchange
- [ ] Incremental daily append after the US close (~23:00 UTC); one-time bulk backfill
- [ ] Volatility indices fetched daily: `^VIX` (US) and `^VSTOXX` (Eurostoxx)
      via yfinance; `VIXCLS` cross-check from FRED
- [ ] `data/sources/fred.py` — `DGS10`, `BAMLH0A0HYM2`, `CPIAUCSL`
- [ ] `data/sources/ecb.py` — deposit rate, EUR/USD
- [ ] Tests: mocked HTTP/yfinance responses, schema + UTC index asserted

### 1.3b Realized volatility (derived)
- [ ] `data/processed/volatility.py` — rolling annualised realized volatility
      (20d and 60d windows) from adjusted close returns → `volatility.parquet`
- [ ] `get_volatility` MCP tool returns per-ticker realized vol plus market
      implied vol (VIX / VSTOXX) for the same window
- [ ] Test: known return series → known realized vol output

### 1.4 News ingest (RSS-first)
- [ ] `data/sources/rss.py` — Google News RSS per ticker, Yahoo Finance RSS per
      ticker, company newsroom feeds (where available)
- [ ] `data/sources/sec.py` — SEC EDGAR filings (8-K, 10-K, proxy) via full-text
      search API/RSS
- [ ] Raw articles written to `data/raw/news/{date}.ndjson` (immutable)
- [ ] `url_hash`/`content_hash` (sha256) for Level-1 dedup
- [ ] Source credibility + recency decay pre-score (`agent/ingestion.py`)
- [ ] Tests: mocked feeds, dedup keys computed, schema asserted

### 1.5 Fact extraction
- [ ] Prompt template `agent/prompts/fact_extraction.md`
- [ ] LLM extracts facts (or returns `REDUNDANT`); writes to `ticker_facts` and
      embeds in ChromaDB; superseded facts get `valid_until`
- [ ] `agent/knowledge.py` — `get_current_facts(as_of)` / `write_fact` /
      `search_similar_facts`, all `as_of`-enforced
- [ ] Tests: mocked LLM, in-memory DuckDB + ChromaDB, boundary cases

### 1.6 Market views
- [ ] `market_views` table: free-text snapshots with `as_of`, `scope`
      (`market`/`ticker`/`theme`), `model`, `data_cutoff` — immutable once written
- [ ] MCP write tool `save_market_view` — the harness writes views incrementally
- [ ] `get_market_views(as_of)` returns historical views point-in-time
- [ ] Tests: write + read round-trip, as-of filtering

### 1.7 Structured extraction job
- [ ] Prompt template `agent/prompts/view_structuring.md`
- [ ] Job reads new free-text views, LLM produces structured JSON into
      `market_view_structures` (schema_version `v1` — stance, thesis, risks,
      catalysts, confidence, related tickers)
- [ ] Free text is never replaced — structured data is derived and versioned
- [ ] Re-run safe: already-structured views are skipped
- [ ] Tests: mocked LLM, idempotent re-runs

### 1.8 MCP server
- [ ] `server/mcp.py` — FastMCP server exposing:
  - Read: `get_prices`, `get_volatility`, `get_news`, `get_facts`,
    `get_market_views`, `search_news`
  - Write: `save_market_view`, `save_fact`
  - Job: `run_ingest` (trigger a daily pipeline run)
- [ ] stdio transport by default; HTTP transport for the Hermes box
- [ ] Every tool has a test with an in-memory database; no live calls in tests

### 1.9 Audit + journal
- [ ] Every job run logged to `agent_runs`
- [ ] Daily summary appended to `journal/{YYYY-MM-DD}.md` (Obsidian-compatible)

**Phase 1 done when:** daily cron on the Hermes box fetches prices + news, facts
are extracted, the harness queries and writes through MCP, the structuring job
runs unattended, and the full test suite is green offline.

## Phase 2 — Backtesting & strategy generation (parked)

Picked up once the market memory is stable. The existing conventions stay:

- Factor library with the standard interface (z-scored, winsorised signals)
- Backtest engine with the six integrity rules (no look-ahead, no survivorship
  bias, walk-forward, transaction costs, multiple-testing correction, IC/IC-decay
  reporting)
- Portfolio construction defaults (risk-parity, half-Kelly, hard constraints,
  monthly rebalancing)
- Strategy generation as a harness-facing capability that consumes facts, views,
  and structured view data — never raw sentiment

## Deployment

- **Target:** the Hermes Hetzner box (`hermes_hetzner`), alongside the harness —
  not a separate stack
- Cron: daily price/macro fetch ~23:00 UTC; news ingest + view structuring after
- MCP: registered as a tool provider by the harness (my.life app or agent runtime);
  HTTP transport on the box, stdio locally
- Notifications: the harness's existing Telegram digest, not a second bot
- Secrets: shared `.env` on the box (never committed)
- Backups: DuckDB file backed up with the rest of the box

## Milestones

| Milestone | Description | Definition of done |
|---|---|---|
| M1: Rescoped scaffold | Phase 1 docs + schema with market views | `init.py` idempotent, ruff + pytest green |
| M2: Universe + DB | S&P 500 + Eurostoxx 600 loaded point-in-time | `get_universe(as_of)` returns correct names |
| M3: Daily prices | Prices + macro growing daily | Two consecutive daily fetches append cleanly |
| M4: News pipeline | RSS ingest + dedup + facts in DuckDB | One day ingested end-to-end |
| M5: MCP live | Harness queries/writes through MCP | Tools exercised from the harness |
| M6: Views + structuring | Views written, structured job runs | Structured rows exist for every free-text view |
| M7: 30-day run | Unattended on the Hermes box | No unhandled errors in `agent_runs`, digest daily |
