# AGENTS.md

## Project

marketmind is a market-memory service for Hermes. It stores historical equity
prices, benchmarks, macro observations, news, filings, versioned facts, and
immutable market views. Market data and writes are exposed through MCP. A
separate, read-only operations dashboard shows job history and logs.

Production runs on one Linux host with Docker Compose and a published image;
the VPS does not need a source checkout. A versioned GitHub release bundles
Compose and host scripts with the application and optional PostgreSQL image
digests. The normal deployment connects to a shared PostgreSQL instance using
a dedicated database and role; an override can create a managed instance when
none exists. PostgreSQL with pgvector is the only structured, queryable store. systemd
timers invoke one-off jobs from the same image as the long-running MCP server.
Local development and CI use a disposable PostgreSQL database.

## Commands

```bash
docker compose up -d                                  # disposable test database
uv run python -m data.db.init_postgres               # apply schema/migrations
uv run pytest tests/ -v --cov=. --cov-report=term-missing
uv run ruff check . && uv run ruff format --check .
uv run python -m data.universe.update
uv run python -m data.universe.resolve
uv run python data/fetch.py
uv run python -m agent.ingestion {YYYY-MM-DD}
uv run python -m agent.views {YYYY-MM-DD}
uv run python -m data.filings.fetch
uv run python -m data.filings.backfill --financials
uv run python -m agent.refresh {YYYY-MM-DD}          # complete daily job
uv run python -m server.mcp                           # authenticated HTTP MCP
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
# Add -f docker-compose.managed-db.yml only when this project owns PostgreSQL.
```

Always use `uv run` for Python commands. In containers and systemd jobs, use
`uv run --frozen --no-sync`. The production runbook is `deploy/README.md`;
`docker-compose.yml` is only the disposable local test database.

## Layout and storage

- `data/db/schema_postgres.sql` is the canonical schema for fresh databases.
  Existing databases advance through immutable files in
  `data/db/postgres_migrations/`, applied by `data/db/init_postgres.py`.
  Repository modules in `data/db/` own SQL.
- `data/raw/` holds immutable Parquet/NDJSON downloads. `state/` holds mutable
  production universe snapshots, derived series, refresh status, locks, and
  logs. Never overwrite archived source files during a refresh.
- `data/sources/`, `data/universe/`, `data/filings/`, and
  `data/processed/` implement ingestion and processing.
- `agent/ingestion.py`, `agent/views.py`, and `agent/refresh.py` run
  extraction, view structuring, and the daily chain. Prompt templates live
  in `agent/prompts/*.md`.
- `server/mcp.py` is the market-data and write interface. `dashboard/`
  serves only run history and logs through a separate read-only database role.

## Code and data conventions

- Use `from __future__ import annotations` and annotate function signatures.
  Public functions need a one-line summary and relevant Args/Returns/Raises.
  Use `loguru.logger` in library code, not `print()`. Keep lines within
  100 columns.
- Use `polars` for ETL and large transforms; use `pandas` for interfaces
  that require it. DataFrame time indexes must be timezone-aware UTC; column
  names are `snake_case`. Validate loaded schemas explicitly.
- Use only free market-data sources: Yahoo Finance, public RSS feeds, SEC
  EDGAR, FRED, ECB, official universe lists, and free identifier services.
  Optional sources cannot become required for the daily path.
- Preserve point-in-time semantics. Resolve identifiers and universe
  membership as of the requested date. Query facts with
  `valid_from <= as_of` and `(valid_until IS NULL OR valid_until > as_of)`.
  Facts are versioned: close the old window and insert a new row.
- Fact embeddings live in PostgreSQL through pgvector. Keep view free text
  immutable; structured view JSON is derived and versioned by
  `schema_version`.
- Put schema changes in the canonical schema and a corresponding numbered
  migration. Never issue ad hoc `ALTER TABLE` from application code.

## Interfaces and jobs

- MCP uses authenticated streamable HTTP, published on a loopback host port.
  Every request needs `MARKETMIND_MCP_TOKEN`. Tool docstrings must describe
  arguments and returned rows.
- The optional operations dashboard uses the `dashboard` Compose profile.
  It may read only `agent_runs` and selected job logs. Its container receives
  only its own DSN/password and browser token, never the main database
  password or source credentials. Publish it on loopback; remote access goes
  through an encrypted, restricted tunnel.
- Scheduled data jobs share `state/refresh/daily-refresh.lock`. Preserve
  `agent_runs` audit rows and terminal status for every run.
- `agent.catchup` processes missed daily dates from the last successful audit
  row, up to seven days. Longer gaps require an operator-selected backfill.
- Keep credentials in untracked `secrets/` files, mounted only into the service
  that needs each one. `.env` contains Compose interpolation and non-secret
  settings. libpq passwords use owner-only `.pgpass` files.

## Testing and release checks

- Keep tests offline: mock external HTTP/model calls and use fixtures for
  market data. PostgreSQL tests use a disposable database whose name ends
  in `_test`; never point them at production.
- Run PostgreSQL-backed tests with `MARKETMIND_REQUIRE_POSTGRES=1` before
  deployment so a missing server fails rather than skipping tests. CI
  requires at least 85% total coverage.
- Check point-in-time boundaries, view round-trips and structuring
  idempotence, volatility calculations, MCP tools, and deployment config
  when changing those paths.
- Before enabling data-job timers on a new host, verify authenticated MCP
  initialize and inspect service logs. Keep the backup timer disabled until
  an offsite destination is configured and a backup set has been restored into
  an isolated database and file directory. Production readiness also requires
  clean consecutive daily cycles and terminal audit rows.

## Naming and hard constraints

- Use neutral, concrete wording in branch names, commits, PRs, docs, and
  comments. Avoid model or orchestration branding in project communications.
- Historical data only. No live feeds, paper trading, execution layer,
  options, derivatives, or paid market-data dependencies.
- MCP remains the only interface for market data and writes. The dashboard
  is restricted to operational status and logs.
- Do not commit `.env`, `secrets/`, raw data, `state/`, database dumps, or notebooks
  with embedded outputs.
