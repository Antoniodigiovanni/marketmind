# marketmind

**Market-memory microservice for agent harnesses — S&P 500 + Eurostoxx 600.**

marketmind owns a growing database of market data — daily closing prices for
~1,100 US and European equities, news, extracted facts, and historical market
views — and exposes that database to an agent harness (Hermes) through **MCP**.
The harness is both a consumer and a writer: it reads prices, news, facts, and
views through MCP tools, and writes free-text market snapshots back into the
database, which a structuring job then turns into queryable JSON.

This is **not a standalone app**. There is no UI, no HTTP API, and no separate
deployment stack. marketmind runs as a service on the same box as the harness,
shares its cron schedule and secrets, and is driven exclusively through the MCP
server in `server/mcp.py`.

## Status

- **Phase 1 — market memory (current).** Price/macro storage, universe
  maintenance, the MCP server, fact store, and market views are in place.
  Several daily jobs are still being built (see [ROADMAP.md](ROADMAP.md)).

## Architecture

```
Hermes / agent harness
        │  MCP (stdio)
        ▼
   marketmind
   ├── server/mcp.py     read/write tools over the database
   ├── daily jobs        universe refresh · price + macro fetch · news ingest · view structuring
   └── storage
       ├── DuckDB        structured data: facts, views, audit (data/db/research.duckdb)
       ├── Parquet       immutable raw prices/macro (data/raw/) and derived series (data/processed/)
       ├── NDJSON        raw news articles, one file per date (data/raw/news/)
       └── ChromaDB      fact embeddings for semantic retrieval (data/db/vectors/)
```

Key principles:

- **Raw data is immutable.** Anything under `data/raw/` is never edited after
  download; derived series live in `data/processed/`.
- **Facts are versioned, never mutated.** Superseding a fact closes the old row
  with `valid_until` and inserts a new one. Every query must be point-in-time
  (`as_of`) to avoid look-ahead bias.
- **Free-text views are immutable.** Structured JSON is derived from them, never
  the other way around.
- **MCP is the only harness-facing interface.**
- **Free data sources only.** No paid APIs.

## Repository layout

```
marketmind/
├── data/
│   ├── raw/            immutable: prices/*.parquet, macro/*.parquet, news/{date}.ndjson
│   ├── processed/      derived series, e.g. volatility.parquet
│   ├── universe/       point-in-time constituents: sp500.csv, eurostoxx600.csv, delisted.csv
│   ├── db/
│   │   ├── schema.sql  DuckDB schema — the single source of truth
│   │   ├── init.py     creates research.duckdb from schema.sql
│   │   └── vectors/    ChromaDB embeddings (not committed)
│   └── sources/        yfinance.py, fred.py, ecb.py, sec.py, rss.py
├── agent/
│   ├── ingestion.py    daily news → fact pipeline
│   ├── views.py        market view write + structured extraction job
│   ├── knowledge.py    fact store read/write (as_of-enforced)
│   └── prompts/        all prompt templates as .md files
├── server/
│   └── mcp.py          the only harness-facing interface
├── journal/            Obsidian-compatible daily notes
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

# Create / verify the DuckDB database
uv run python -m data.db.init

# Run the test suite (in-memory DuckDB, mocked APIs, no network)
uv run pytest tests/ -v --cov=. --cov-report=term-missing
```

### Environment variables

Loaded from `.env` via `python-dotenv`; never committed.

| Variable | Purpose |
|---|---|
| `AI_API_KEY` | Chat API key (DeepSeek-compatible endpoint) for extraction / structuring |
| `LLM_BASE_URL` | Chat endpoint (default `https://api.deepseek.com`) |
| `LLM_MODEL` | Model identifier (default `deepseek-chat`) |
| `LLM_MAX_RETRIES` | Retry attempts for transient chat API failures (default 3) |
| `LLM_TIMEOUT_SECONDS` | Chat API request timeout (default 60) |
| `FRED_API_KEY` | FRED macro series |
| `INGEST_MAX_CONSECUTIVE_FAILURES` | Consecutive extraction failures before the ingest run aborts (default 20) |

## Commands

```bash
uv run python -m data.db.init                 # create/verify research.duckdb
uv run python -m data.universe.update         # refresh S&P 500 / STOXX 600 lists
uv run python -m data.universe.resolve        # resolve symbols into symbol_map
uv run python data/fetch.py                   # daily price + macro refresh
uv run python -m agent.ingestion {YYYY-MM-DD} # daily news + fact ingest
uv run python -m agent.views {YYYY-MM-DD}     # structure free-text market views
uv run python -m server.mcp                   # run the MCP server (stdio)
uv run ruff check . && uv run ruff format .   # lint + format
```

The view-structuring extractor is still a stub (raises `NotImplementedError`
until the corresponding roadmap item lands). The daily price/macro fetch,
universe refresh, symbol resolution, news ingest, filings pipeline, company
profiles, and MCP server are functional.

## Deployment (generic VPS)

marketmind is designed to run on the same Linux box as the harness: a
dedicated system user, systemd timers for the daily jobs, and an MCP server
spawned over stdio — **no ports are opened**.

### Requirements

- Linux with systemd (Ubuntu/Debian recommended), root access.
- GitHub access: public clone, or a read-only deploy key for a private repo.
- `uv` — installed automatically by the installer.

### One-command install

```bash
# public repo
curl -fsSL https://raw.githubusercontent.com/Antoniodigiovanni/marketmind/main/deploy/install.sh | sudo bash

# private repo (clone once, then run locally)
git clone git@github.com:<you>/marketmind.git
cd marketmind
sudo REPO_URL=git@github.com:<you>/marketmind.git bash deploy/install.sh
```

For a private repo, create a **read-only deploy key** (GitHub → repo →
Settings → Deploy keys), place the private half at `/root/.ssh/marketmind_deploy`
on the VPS, and add the `Host github.com` config entry so root's git uses it —
full recipe in [deploy/README.md](deploy/README.md).

The installer is idempotent: it creates the `marketmind` system user, clones
the repo to `/srv/marketmind`, installs `uv`, runs `uv sync --frozen`,
installs `.env`, initialises the DuckDB schema, installs hardened systemd
units, and enables the timers.

### Secrets

Prepare a `.env` and pass it to the installer:

```bash
sudo ENV_FILE=/root/marketmind.env \
     REPO_URL=git@github.com:<you>/marketmind.git \
     bash /srv/marketmind/deploy/install.sh
```

Required keys: `AI_API_KEY` (chat API), `FRED_API_KEY`. The installed
`/srv/marketmind/.env` is mode `0600` and is **never touched** by installs or
updates. The installer **aborts** if no `.env` exists or if it still holds the
example placeholders — enabling the timers with an unusable key would give you
a job that fails silently every morning.

### Schedules (systemd timers)

| Timer | Schedule | Job |
|---|---|---|
| `marketmind-daily.timer` | daily 06:10 | price/macro fetch + news ingest |
| `marketmind-universe.timer` | Mondays 06:00 | universe refresh + symbol resolution |
| `marketmind-profiles.timer` | annually, 5 Jan | company profile refresh (`--backfill --profiles-only`) |
| `marketmind-backup.timer` | nightly 02:30 | `data/` tarball, 14-day retention |
| `marketmind-update.timer` | Mondays 05:30 (opt-in) | self-update |

Override a schedule with `sudo systemctl edit <timer>` — overrides survive
updates.

### Updating

```bash
sudo /usr/local/sbin/marketmind-update
```

Pulls the configured branch/tag (read from `/srv/marketmind/deploy.conf`),
re-syncs dependencies, migrates the schema, reinstalls units, and re-arms
timers. Opt into weekly unattended updates with `ENABLE_AUTO_UPDATE=1` in
`deploy.conf`.

### MCP wiring for the harness

The harness spawns the server as the `marketmind` user:

```bash
sudo -u marketmind env HOME=/srv/marketmind UV_CACHE_DIR=/srv/marketmind/.cache \
  bash -c 'cd /srv/marketmind && exec /usr/local/bin/uv run --frozen --no-sync python -m server.mcp'
```

### Health checks

```bash
systemctl list-timers | grep marketmind
journalctl -u marketmind-daily.service -e
```

Every job also writes an audit row to `agent_runs` (counts + `error` column).

### Backups

The nightly timer tarballs `data/` to `/srv/marketmind-backups/`. Move it
off-box (rclone/restic) — a same-machine copy is a local safety net, not
disaster recovery.

Full operational detail lives in [deploy/README.md](deploy/README.md).

## MCP tools

The MCP server (`server/mcp.py`) exposes the following tools:

| Tool | Args | Returns |
|---|---|---|
| `get_prices` | `ticker`, `start?`, `end?` | Daily `{date, open, high, low, close, volume}` |
| `get_volatility` | `ticker`, `start?`, `end?` | `{date, ticker, rv_20d, rv_60d}` (annualised %) or vol-index close for `^VIX` / `^VSTOXX` |
| `get_news` | `ticker`, `since?`, `limit?` | Recent articles `{url, source, ticker, published_at, title, description}` |
| `search_news` | `query`, `limit?` | Full-text news search, recency-ranked |
| `get_facts` | `ticker`, `as_of` | Point-in-time facts valid on `as_of` |
| `get_market_views` | `as_of`, `ticker?`, `scope?`, `limit?` | Views for `as_of` or earlier, newest first |
| `save_market_view_tool` | `as_of`, `view_text`, `model`, `scope`, `ticker?`, `theme?`, `data_cutoff?` | New immutable view ID |
| `save_fact` | `ticker`, `statement`, `fact_type`, `confidence`, `valid_from`, `source_article_id`, `supersedes_id?` | New versioned fact ID |
| `run_ingest` | `date` | Runs the daily news + fact ingest pipeline |

## Storage model

Structured, queryable data lives in DuckDB (`data/db/research.duckdb`), created
from `data/db/schema.sql` — the single source of truth for the schema.

| Table | Purpose |
|---|---|
| `articles` | Deduplication registry and ingest audit trail (sha256 URL/content hashes) |
| `ticker_facts` | Versioned knowledge base — one row per extracted fact |
| `agent_runs` | Audit log for every job execution |
| `market_views` | Free-text market snapshots written by the harness, immutable |
| `market_view_structures` | Structured JSON derived from free-text views (`schema_version`-keyed) |

### Point-in-time queries

Always query facts as-of a date:

```sql
SELECT statement FROM ticker_facts
WHERE ticker = :ticker
  AND valid_from <= :as_of
  AND (valid_until IS NULL OR valid_until > :as_of);
```

Querying without `:as_of` is a look-ahead bias violation. Use
`agent.knowledge.get_current_facts(ticker, as_of=...)` — it enforces this —
never raw SQL in research code.

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
- Tests run in <60 s with **no network access**: in-memory DuckDB and mocked
  API responses. `agent/tools.py` and `server/mcp.py` target 100% coverage.

## Further reading

- [AGENTS.md](AGENTS.md) — project conventions, commands, and hard constraints
- [ROADMAP.md](ROADMAP.md) — phase plan and current status
- [docs/](docs/) — design specs and implementation plans
