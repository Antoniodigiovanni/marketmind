# AGENTS.md

## Project

marketmind is a market-memory service for Hermes. It stores historical equity
prices, benchmarks, macro observations, news, filings, versioned facts, and
immutable market views. MCP is the market-data and write interface. An
optional read-only web app shows job history, logs, and MCP-backed data pages.

Production runs on Debian/Ubuntu with versioned native Python releases and
systemd services/timers; Docker is not required on the host. A versioned
GitHub release bundles the application, locked dependencies, and host scripts.
The normal deployment connects to an existing PostgreSQL instance with a
dedicated database and role. On a new host, the installer can explicitly offer
to install PostgreSQL with pgvector through apt. PostgreSQL remains an
independent host service; Marketmind does not own its lifecycle. Later projects
receive their own databases and roles. PostgreSQL with pgvector is the only
structured, queryable store. systemd timers run jobs from the same versioned
release as the long-running MCP server.
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
uv run python -m data.filings.esef
uv run python -m agent.refresh {YYYY-MM-DD}          # complete daily job
uv run python -m server.mcp                           # authenticated HTTP MCP
./deploy/install.sh                                   # first host install (release archive)
./deploy/dashboard.sh enable|disable|status           # dashboard lifecycle
marketmind update                                    # install newest production release
marketmind update vX.Y.Z                             # install a selected release
# PostgreSQL is an independent host service or an existing remote server.
```

Always use `uv run` for development Python commands. Native systemd jobs use
the locked virtual environment in `releases/current`. The production runbook
is `deploy/README.md`; `docker-compose.yml` is only the disposable local test
database.

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
  serves job history and logs through a read-only database role, and retrieves
  market data through MCP; it must not query market tables directly.

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
- The optional web app runs as a separate systemd service. It may read
  `agent_runs` and selected job logs through its SELECT-only role; market-data
  pages use authenticated MCP tools. Its container receives only its read-only
  DSN/password and MCP credential, never the main database password or source
  credentials. Publish it on loopback; remote access goes through an encrypted,
  restricted tunnel or private reverse proxy. `dashboard.sh` may configure a
  private Tailscale Serve mapping, but never Tailscale Funnel.
- `deploy/install.sh` owns first-host configuration. Routine releases use
  `deploy/update.sh`, which preserves runtime configuration, credentials,
  state, timer choices, and the independent PostgreSQL service. Dashboard
  lifecycle operations go through `deploy/dashboard.sh`.
- Scheduled data jobs share `state/refresh/daily-refresh.lock`. Preserve
  `agent_runs` audit rows and terminal status for every run.
- The daily refresh ends with the incremental SEC filings fetch. European ESEF
  indexes run monthly because each pass re-scans country indexes.
- `agent.catchup` processes missed daily dates from the last successful audit
  row, up to seven days. Longer gaps require an operator-selected backfill.
- Keep credentials in untracked `secrets/` files, exposed only to the service
  that needs each one. `.env` contains only non-secret settings. libpq passwords
  use owner-only `.pgpass` files.

## Testing and release checks

- Keep tests offline: mock external HTTP/model calls and use fixtures for
  market data. PostgreSQL tests use a disposable database whose name ends
  in `_test`; never point them at production.
- Live source contracts are a separate integration suite. Run them with
  `RUN_LIVE_TESTS=1 uv run pytest tests/integration -m live --no-cov`; SEC and
  filings.xbrl.org require real `SEC_CONTACT_NAME` and `SEC_CONTACT_EMAIL`
  values, while the FRED and chat checks require their configured credentials.
  The suite checks each external provider separately. A scheduled workflow
  detects upstream changes, and release builds must pass all live checks before
  publishing.
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
- MCP remains the only interface for market data and writes. The web app may
  present MCP results but must not query market tables or write directly.
- Do not commit `.env`, `secrets/`, raw data, `state/`, database dumps, or notebooks
  with embedded outputs.
