# marketmind

**Market-memory microservice for Hermes — S&P 500 + Eurostoxx 600.**

marketmind owns a growing database of market data — daily closing prices for
~1,100 US and European equities, news, extracted facts, and historical market
views — and exposes that database to Hermes through **MCP**.
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
   ├── daily jobs        universe refresh · price + macro fetch · news ingest · view structuring
   └── storage
       ├── PostgreSQL    identity, prices, news, facts, views, filings, audit
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
```

### Environment variables

`.env` is never committed. In production, `docker compose`'s `env_file:`
directive loads it into the app container (see `docker-compose.prod.yml`);
locally, export the values in your shell or pass `--env-file .env` to
`uv run` yourself — no code in this repo calls `python-dotenv`.

| Variable | Purpose |
|---|---|
| `AI_API_KEY` | Chat API key (DeepSeek-compatible endpoint) for extraction / structuring |
| `LLM_BASE_URL` | Chat endpoint (default `https://api.deepseek.com`) |
| `LLM_MODEL` | Model identifier (default `deepseek-chat`) |
| `LLM_MAX_RETRIES` | Retry attempts for transient chat API failures (default 3) |
| `LLM_TIMEOUT_SECONDS` | Chat API request timeout (default 60) |
| `FRED_API_KEY` | FRED macro series |
| `SEC_CONTACT_NAME` | Operator name sent to SEC EDGAR and filings.xbrl.org (required) |
| `SEC_CONTACT_EMAIL` | Operator email sent with every EDGAR request (required) |
| `INGEST_MAX_CONSECUTIVE_FAILURES` | Consecutive extraction failures before the ingest run aborts (default 20) |
| `INGEST_PRE_SCORE_MIN` | Minimum credibility/recency score sent to extraction (default 0.30) |
| `INGEST_MAX_NEWS_PER_FEED` | Maximum accepted articles from each company feed (default 30) |
| `MARKETMIND_DATABASE_DSN` | Postgres connection string; unset falls back to the local docker-compose test database |
| `MARKETMIND_DB_PASSWORD` | Production Postgres password, supplied to libpq separately from the DSN |
| `MARKETMIND_MCP_TOKEN` | Bearer token the MCP client must present on every request |
| `MARKETMIND_MCP_PORT` | Port the MCP server listens on (default 8000) |
| `MARKETMIND_REFRESH_DIR` | Shared refresh lock/status/log directory (production: `/var/lib/marketmind/refresh`) |
| `MARKETMIND_UID` / `MARKETMIND_GID` | Numeric owner used by the production app container for its bind-mounted data tree |

## Commands

```bash
uv run python -m data.db.init_postgres        # apply PostgreSQL schema/migrations
uv run python -m data.universe.update         # refresh S&P 500 / STOXX 600 lists
uv run python -m data.universe.resolve        # resolve symbols into symbol_map
uv run python data/fetch.py                   # daily price + macro refresh
uv run python -m agent.ingestion {YYYY-MM-DD} # daily news + fact ingest
uv run python -m agent.views {YYYY-MM-DD}     # structure free-text market views
uv run python -m agent.refresh {YYYY-MM-DD}   # complete lock-protected daily pipeline
uv run python -m server.mcp                   # run the MCP server (streamable-http, 127.0.0.1:8000)
uv run ruff check . && uv run ruff format .   # lint + format
```

The daily price/macro fetch, universe refresh, symbol resolution, news ingest,
market-view structuring, filings pipeline, company profiles, and MCP server are
functional.

## Deployment (generic VPS)

Docker-native: one image for the whole app, `docker compose` for the
always-on services (Postgres, the MCP server over streamable-http — no
ports beyond `127.0.0.1`), plain cron for scheduled jobs. No systemd, no
install/update scripts.

```bash
deploy_user="$(id -un)"
deploy_group="$(id -gn)"
sudo install -d -m 0755 -o "$deploy_user" -g "$deploy_group" \
  /srv/marketmind
sudo install -d -m 0700 -o root -g root \
  /srv/marketmind-backups /var/log/marketmind
git clone git@github.com:Antoniodigiovanni/marketmind.git /srv/marketmind
cd /srv/marketmind
install -d -m 0700 data/raw state state/universe state/processed state/refresh
cp .env.example .env   # edit: real secrets, database settings, MCP token, host UID/GID
chmod 0600 .env

docker compose -f docker-compose.prod.yml up -d --build
sudo install -o root -g root -m 0644 deploy/crontab /etc/cron.d/marketmind
sudo install -o root -g root -m 0644 deploy/logrotate /etc/logrotate.d/marketmind
```

Updating pulls the checkout, rebuilds Compose, and reinstalls
`/etc/cron.d/marketmind`; the app applies checksum-verified PostgreSQL
migrations on startup. See the deployment guide for the exact commands.

Full operational detail — schedule table, MCP client wiring (the client
now connects to `http://127.0.0.1:8000/mcp` as a streamable-http client
with a bearer token, rather than spawning a stdio subprocess), operations,
and rollback — lives in [deploy/README.md](deploy/README.md).

## MCP tools

The MCP server (`server/mcp.py`) exposes the following tools:

| Tool | Args | Returns |
|---|---|---|
| `get_prices` | `ticker`, `start?`, `end?` | Daily `{date, open, high, low, close, volume}` |
| `get_volatility` | `ticker`, `start?`, `end?` | `{date, ticker, rv_20d, rv_60d}` (annualised %) or vol-index close for `^VIX` / `^VSTOXX` |
| `get_news` | `ticker`, `since?`, `limit?` | Recent articles `{url, source, publisher, ticker, published_at, title, summary}` |
| `search_news` | `query`, `limit?` | Full-text news search, recency-ranked |
| `get_facts` | `ticker`, `as_of` | Point-in-time facts valid on `as_of` |
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

`run_daily_refresh` runs the same ordered entry point as cron and returns
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
| `agent_runs` | Audit log for every job execution (PostgreSQL) |
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
