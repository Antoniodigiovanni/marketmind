# marketmind

**Market-memory microservice for Hermes — S&P 500 + Eurostoxx 600.**

marketmind owns a growing database of market data — daily closing prices for
~1,100 US and European equities, market and sector benchmarks, news, extracted
facts, and historical market views — and exposes that database to Hermes
through **MCP**.
The MCP client is both a consumer and a writer: it reads prices, news, facts, and
views through MCP tools, and writes free-text market snapshots back into the
database, which a structuring job then turns into queryable JSON.

This is **not a standalone app**. There is no UI or independent public
deployment. marketmind runs as a service on the same box as Hermes and is
driven exclusively through the MCP server in `server/mcp.py`, over
streamable-http bound to `127.0.0.1` — reachable only from that box, not a
public HTTP API.

## Status

- **Phase 1 — market memory (current).** Price/macro storage, universe
  maintenance, the MCP server, fact store, and market views are in place.
  Several daily jobs are still being built (see [ROADMAP.md](ROADMAP.md)).

## Architecture

```
Hermes
        │  MCP (streamable-http, 127.0.0.1, bearer token)
        ▼
   marketmind
   ├── server/mcp.py     read/write tools over the database
   ├── daily jobs        universe refresh · equity/benchmark prices · macro · news · views
   └── storage
       ├── PostgreSQL    identity, prices, news, facts, financials, views, filings, audit
       ├── Parquet       immutable raw data (data/raw/) and derived series (state/processed/)
       ├── NDJSON        raw news articles, one file per date (data/raw/news/)
       └── pgvector      fact embeddings inside PostgreSQL
```

Key principles:

- **Raw data is immutable.** Anything under `data/raw/` is never edited after
  download; production-derived series live in `state/processed/`.
- **Facts are versioned, never mutated.** Superseding a fact closes the old row
  with `valid_until` and inserts a new one. Every query must be point-in-time
  (`as_of`) to avoid look-ahead bias.
- **Free-text views are immutable.** Structured JSON is derived from them, never
  the other way around.
- **MCP is the only client-facing interface.**
- **Free data sources only.** No paid APIs.

## Repository layout

```
marketmind/
├── data/
│   ├── raw/            immutable: prices/*.parquet, macro/*.parquet, news/{date}.ndjson
│   ├── processed/      derived series, e.g. volatility.parquet
│   ├── universe/       point-in-time constituents: sp500.csv, eurostoxx600.csv, delisted.csv
│   ├── db/
│   │   ├── schema_postgres.sql  canonical PostgreSQL schema
│   │   ├── postgres_migrations/ versioned PostgreSQL transitions
│   │   └── init_postgres.py  applies the schema and pending migrations
│   └── sources/        yfinance.py, fred.py, ecb.py, sec.py, rss.py
├── agent/
│   ├── ingestion.py    daily news → fact pipeline
│   ├── views.py        market view write + structured extraction job
│   ├── knowledge.py    fact store read/write (as_of-enforced)
│   └── prompts/        all prompt templates as .md files
├── server/
│   └── mcp.py          the only client-facing interface
├── journal/            Obsidian-compatible daily notes
├── state/              derived files, universe snapshots, refresh state (not committed)
└── docs/               design specs and implementation plans
```

## Getting started

Requirements: Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/). Always use
`uv run` — never bare `python` or `pip`.

```bash
# Install dependencies
uv sync

# Configure secrets (see .env.example)
cp .env.example .env

# Start the disposable PostgreSQL database and apply the schema
docker compose up -d
uv run python -m data.db.init_postgres

# Run the test suite (disposable PostgreSQL, mocked external APIs)
uv run pytest tests/ -v --cov=. --cov-report=term-missing

# Verify live SEC response contracts (requires real SEC contact variables)
RUN_LIVE_TESTS=1 uv run pytest tests/integration/test_sec_live.py -m live --no-cov
```

### Environment variables

`.env` is never committed. In production it contains non-secret Compose
settings; credentials live in untracked `secrets/` files mounted only into
services that need them. Locally, export values in your shell or pass
`--env-file .env` to `uv run` yourself — no code calls `python-dotenv`.

| Variable | Purpose |
|---|---|
| `MARKETMIND_IMAGE` | Published image reference shared by the MCP service, dashboard, and scheduled jobs |
| `AI_API_KEY_FILE` | Mounted chat API key for extraction / structuring in production |
| `LLM_BASE_URL` | Chat endpoint (default `https://api.deepseek.com`) |
| `LLM_MODEL` | Model identifier (default `deepseek-chat`) |
| `LLM_MAX_RETRIES` | Retry attempts for transient chat API failures (default 3) |
| `LLM_TIMEOUT_SECONDS` | Chat API request timeout (default 60) |
| `FRED_API_KEY_FILE` | Mounted FRED key in production |
| `SEC_CONTACT_NAME` | Operator name sent to SEC EDGAR and filings.xbrl.org (required) |
| `SEC_CONTACT_EMAIL` | Operator email sent with every EDGAR request (required) |
| `INGEST_MAX_CONSECUTIVE_FAILURES` | Consecutive extraction failures before the ingest run aborts (default 20) |
| `INGEST_PRE_SCORE_MIN` | Minimum credibility/recency score sent to extraction (default 0.30) |
| `INGEST_MAX_NEWS_PER_FEED` | Maximum accepted articles from each company feed (default 30) |
| `MARKETMIND_DATABASE_DSN` | Postgres connection string; unset falls back to the local docker-compose test database |
| `MARKETMIND_MCP_TOKEN_FILE` | Path to mounted MCP bearer token in production |
| `MARKETMIND_MCP_PORT` | Port the MCP server listens on (default 8000) |
| `MARKETMIND_REFRESH_DIR` | Shared refresh lock/status/log directory (production: `/var/lib/marketmind/refresh`) |
| `MARKETMIND_UID` / `MARKETMIND_GID` | Numeric owner used by the production app container for its bind-mounted data tree |
| `MARKETMIND_DASHBOARD_TOKEN_FILE` | Path to mounted dashboard HTTP Basic password |
| `MARKETMIND_DASHBOARD_DSN` | SELECT-only Postgres DSN used by the dashboard |
| `PGPASSFILE` | Owner-only PostgreSQL password file mounted per service |
| `MARKETMIND_DASHBOARD_PORT` | Dashboard listen port (default 8001) |
| `MARKETMIND_DASHBOARD_HOST` | Dashboard bind address (default `127.0.0.1`) |
| `MARKETMIND_DASHBOARD_LOG_DIRS` | Colon-separated directories whose `.log` files the dashboard may serve |

## Commands

```bash
uv run python -m data.db.init_postgres        # apply PostgreSQL schema/migrations
uv run python -m data.universe.update         # refresh S&P 500 / STOXX 600 lists
uv run python -m data.universe.resolve        # resolve symbols into symbol_map
uv run python data/fetch.py                   # daily price + macro refresh
uv run python -m agent.ingestion {YYYY-MM-DD} # daily news + fact ingest
uv run python -m agent.views {YYYY-MM-DD}     # structure free-text market views
uv run python -m data.filings.fetch --run-date {YYYY-MM-DD}  # SEC filings + Company Facts (today when omitted)
uv run python -m data.filings.backfill --financials  # Company Facts backfill / quarterly sweep
uv run python -m data.filings.esef            # European ESEF filings (monthly)
uv run python -m agent.refresh {YYYY-MM-DD}   # complete lock-protected daily pipeline
uv run python -m server.mcp                   # run the MCP server (streamable-http, 127.0.0.1:8000)
uv run python -m dashboard.app                # run the read-only ops dashboard (127.0.0.1:8001)
uv run ruff check . && uv run ruff format .   # lint + format
```

The daily price/macro fetch, universe refresh, symbol resolution, news ingest,
market-view structuring, filings pipeline, company profiles, and MCP server are
functional.

## Deployment (generic VPS)

A `v*` release tag builds and publishes one application image and a matching
host configuration archive. The archive includes pinned application and
PostgreSQL image digests. The VPS downloads that archive and pulls the images;
it does not clone or build the source. Compose keeps PostgreSQL and MCP
running; systemd timers launch scheduled jobs from the same pinned image.
Only loopback ports are published. The [deployment guide](deploy/README.md)
covers credentials, an optional offsite backup restore drill, scheduling, and updates.

### Operations dashboard

A read-only web view of `agent_runs` and job logs, in its own container
(`dashboard/`). It shows why a scheduled job failed at 06:10. It runs under a
SELECT-only database role
granted a single table, is HTTP Basic authenticated, and binds `127.0.0.1` —
reach it with an SSH port-forward, or place a TLS-terminating reverse proxy
in front of it. It serves no market data, holds no write path, and is not
consumed by the harness.

Full operational detail — schedule table, MCP client wiring (the client
now connects to `http://127.0.0.1:8000/mcp` as a streamable-http client
with a bearer token, rather than spawning a stdio subprocess), operations,
and rollback — lives in [deploy/README.md](deploy/README.md).

## MCP tools

The MCP server (`server/mcp.py`) exposes the following tools:

| Tool | Args | Returns |
|---|---|---|
| `get_prices` | `ticker`, `start?`, `end?`, `to_currency?` | Daily `{date, open, high, low, close, volume}` for an equity or benchmark Yahoo symbol |
| `get_benchmarks` | `kind?`, `region?` | Configured series metadata and stored date coverage |
| `get_volatility` | `ticker`, `start?`, `end?` | `{date, ticker, rv_20d, rv_60d}` (annualised %) or vol-index close for `^VIX` / `^VSTOXX` |
| `get_news` | `ticker`, `since?`, `limit?` | Recent articles `{url, source, publisher, ticker, published_at, title, summary}` |
| `search_news` | `query`, `limit?` | Full-text news search, recency-ranked |
| `get_facts` | `ticker`, `as_of` | Point-in-time facts valid on `as_of` |
| `get_financials` | `ticker`, `concepts`, `as_of` | Latest SEC XBRL value per concept/unit/fiscal period filed by `as_of` |
| `get_market_views` | `as_of`, `ticker?`, `scope?`, `schema_version?`, `limit?` | Views for `as_of` or earlier with derived structure, newest first |
| `save_market_view_tool` | `as_of`, `view_text`, `model`, `scope`, `ticker?`, `theme?`, `data_cutoff?` | New immutable view ID |
| `save_fact` | `ticker`, `statement`, `fact_type`, `confidence`, `valid_from`, `source_article_id`, `supersedes_id?` | New versioned fact ID |
| `run_ingest` | `date`, `profiles_only?`, `backfill?`, `tickers?` | Starts the daily news + fact ingest in the background; returns a run id |
| `run_filings_refresh` | `run_date?` | Starts the daily SEC filings fetch in the background; returns a run id |
| `get_ingest_status` | `limit?` | The in-flight run (state, processed/total, counts) plus recent `agent_runs` rows |
| `run_daily_refresh` | `as_of?` | Starts the complete daily pipeline in a detached process; returns `{started, run_id, log_path}` |
| `get_refresh_status` | `limit?` | Shared-lock state, persisted refresh outcome, and recent `agent_runs` rows |

### Job tools

`run_ingest` and `run_filings_refresh` take hours over the full universe, so
they return as soon as the run is accepted rather than blocking the call.
Poll `get_ingest_status()` for progress.

Only one run may be in flight at a time; a second call is refused with the id
of the running one. Problems that can be detected up front — a malformed date,
tickers outside the universe, a missing API key — come back as a diagnostic
string and start nothing. Failures during a run (including the consecutive-
failure circuit breaker) are reported through `get_ingest_status`, and the run
is recorded in `agent_runs` either way.

`tickers` narrows the ingest to a subset of the point-in-time universe, which
is the practical way to trigger a small run on demand; the scheduled timer
still covers the full universe.

### Benchmark prices

The daily price refresh incrementally stores 13 free Yahoo series in the same
PostgreSQL `listings` and `prices` tables as equity and volatility-index bars:

- Market anchors: S&P 500 (`^GSPC`) and STOXX Europe 600 (`^STOXX`).
- S&P 500 sectors: Energy (`^SP500-10`), Materials (`^SP500-15`), Industrials
  (`^SP500-20`), Consumer Discretionary (`^SP500-25`), Consumer Staples
  (`^SP500-30`), Health Care (`^SP500-35`), Financials (`^SP500-40`),
  Information Technology (`^SP500-45`), Communication Services (`^SP500-50`),
  Utilities (`^SP500-55`), and Real Estate (`^SP500-60`).

Call `get_benchmarks()` to discover stable keys, Yahoo symbols, classification,
native currency, and the first/last stored dates. Read a series with
`get_prices(symbol, start=..., end=as_of)`; the inclusive `end` cutoff is the
point-in-time boundary. On the first refresh, missing series backfill from
2015-01-01. Later runs start at the last stored date plus one day. Source
failures are isolated and reported in the price-refresh log; they do not stop
the remaining daily steps. Yahoo's free delayed adjusted OHLCV is the only new
source, so no key, paid subscription, schema migration, or separate schedule is
required.

`run_daily_refresh` runs the same ordered entry point as the systemd timer and returns
immediately. Both paths use `state/refresh/daily-refresh.lock`, so a scheduled
run, manual refresh, universe update, profile refresh, or filesystem backup
cannot overlap. Poll `get_refresh_status`; there is deliberately no stop tool
because interrupting immutable Parquet/NDJSON writes can leave partial files.

## Storage model

All structured, queryable data lives in PostgreSQL. The canonical fresh
schema is `data/db/schema_postgres.sql`; immutable transitions for existing
databases live in `data/db/postgres_migrations/`. Parquet and NDJSON remain the
immutable raw/derived archive formats; they are not competing databases.

| Table | Purpose |
|---|---|
| `articles` | Deduplication registry and ingest audit trail (PostgreSQL) |
| `company_facts` | Versioned company knowledge with pgvector embeddings (PostgreSQL) |
| `financials` | SEC XBRL observations keyed by stable company identity, preserving filings and restatements |
| `agent_runs` | Audit log for every job execution (PostgreSQL) — `daily_refresh`, `fetch`, `news`, `views`, `filings`, `profiles` |
| `market_views` | Immutable free-text market snapshots (PostgreSQL) |
| `market_view_structures` | Structured JSON derived from free text (PostgreSQL) |
| `filings`, ownership, 13F stores | Filing metadata, normalized text, and parsed disclosures (PostgreSQL) |

### Point-in-time queries

Always query facts as-of a date:

```sql
SELECT statement FROM company_facts
WHERE company_id = :company_id
  AND valid_from <= :as_of
  AND (valid_until IS NULL OR valid_until > :as_of);
```

Querying without `:as_of` is a look-ahead bias violation. Resolve the ticker
point-in-time, then use `data.db.facts.get_current_facts()`; it requires the
date explicitly. Do not bypass the repository with raw SQL.

Structured financial observations use the same discipline on their filing-time
axis. `data.db.financials.get_financials_as_of()` partitions by concept, unit,
and fiscal period, excludes rows filed after `as_of`, and returns the newest
remaining filing. The accession number on every row identifies its SEC source;
later amendments and restatements append rows instead of replacing history.

The endpoint returns an issuer's complete history on every call, so the daily
job refreshes only issuers that filed a 10-K, 10-Q, 20-F or 6-K in that run.
A quarterly sweep (`data.filings.backfill --financials`, scheduled in
`deploy/systemd/marketmind-financials.timer`) covers XBRL published after its filing and the amendments the
daily job does not fetch.

## Daily jobs

The daily batch runs in order:

1. **Universe refresh** — pulls the official STOXX Europe 600 selection list and
   S&P 500 holdings (iShares CSPX, BlackRock fund API), applies append-only
   changes, and refuses removals when a source shrinks by more than 5%.
2. **Price + macro fetch** — adjusted OHLCV for universe tickers, vol indices
   (`^VIX`, `^VSTOXX`), FRED and ECB series → immutable Parquet in `data/raw/`.
3. **News ingest** — fetch → level-1 dedup → pre-score (source credibility ×
   recency decay) → fact retrieval → LLM extraction → versioned writes → audit.
4. **View structuring** — reads new free-text views and produces structured JSON
   (`schema_version`-driven); free text is never replaced and re-runs are safe.
5. **SEC filings** — stores metadata and normalized filing text, and refreshes
   the free, keyless Company Facts endpoint into bitemporal financial rows for
   issuers that filed a periodic report in that run. Runs last so that an EDGAR
   outage or rate limit cannot hold back the preceding steps.

European ESEF filings (`data.filings.esef`) are **not** part of the daily
pipeline. The job re-scans each country's filing index rather than reading
forward from a checkpoint, so it runs monthly from `deploy/crontab` instead.

Raw news is written to `data/raw/news/{date}.ndjson`; processed volatility
derived from adjusted closes lives in `data/processed/volatility.parquet`.

## Development

```bash
uv run pytest tests/ -v --cov=. --cov-report=term-missing
uv run ruff check . && uv run ruff format .
```

Conventions (full detail in [AGENTS.md](AGENTS.md)):

- Type annotations on all signatures (`from __future__ import annotations`),
  docstrings on public functions, `loguru` instead of `print()`, max line
  length 100.
- `polars` for ETL; all DataFrames use timezone-aware UTC `DatetimeIndex`;
  schemas are asserted explicitly on load.
- Tests run in <60 s with **no external network access**: a disposable local
  PostgreSQL database and mocked API responses. `agent/tools.py` and
  `server/mcp.py` target 100% coverage.

## Further reading

- [AGENTS.md](AGENTS.md) — project conventions, commands, and hard constraints
- [ROADMAP.md](ROADMAP.md) — phase plan and current status
- [docs/](docs/) — design specs and implementation plans
