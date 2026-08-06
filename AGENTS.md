# AGENTS.md

## Project

marketmind is the **market-memory microservice** for an agent harness (Hermes).
It owns a growing database of market data — daily closing prices for the
S&P 500 and Eurostoxx 600 (~1,100 equities), news, extracted facts, and
historical market views — and exposes that database through **MCP** tools.
The harness reads data through MCP and writes free-text market views back into
the database; a job then structures those views for querying.

This is **not a standalone app**. There is no UI, no separate deployment stack,
and no own notification system. marketmind runs as a service on the same box as
the harness, shares its cron schedule and secrets, and is driven through MCP.

All data sources must be free. No paid APIs.

## Commands

```bash
uv run pytest tests/ -v --cov=. --cov-report=term-missing   # run tests
uv run ruff check . && uv run ruff format .                  # lint + format
uv run python -m data.db.init                                # create/verify research.duckdb
uv run python data/fetch.py                                  # daily price + macro refresh
uv run python -m data.universe.update                        # refresh S&P 500 / STOXX 600 lists
uv run python -m agent.ingestion {YYYY-MM-DD}               # daily news + fact ingest
uv run python -m agent.views {YYYY-MM-DD}                   # structure free-text market views
uv run python -m server.mcp                                  # run the MCP server (stdio)
```

Always use `uv run` — never bare `python` or `pip`.

## Repository layout

```
marketmind/
├── data/
│   ├── raw/            ← immutable, never edited after download
│   │   ├── prices/     ← Parquet, one file per ticker
│   │   ├── macro/      ← Parquet, one file per series
│   │   └── news/       ← raw articles as line-delimited JSON, one file per date
│   ├── processed/      ← calendar-aligned derived series
│   ├── universe/       ← point-in-time constituent lists (auto-refreshed from official sources)
│   ├── db/
│   │   ├── schema.sql  ← DuckDB schema (source of truth)
│   │   ├── init.py     ← creates research.duckdb from schema.sql
│   │   └── research.duckdb  ← generated, not committed
│   └── sources/        ← one module per data source
│       ├── yfinance.py ← OHLCV prices
│       ├── rss.py      ← Google News / Yahoo Finance / company feeds
│       ├── sec.py      ← SEC EDGAR filings
│       ├── fred.py     ← FRED macro
│       └── ecb.py      ← ECB SDW (rates, FX)
├── agent/
│   ├── ingestion.py    ← daily news → fact pipeline
│   ├── views.py        ← market view write + structured extraction job
│   ├── knowledge.py    ← fact store read/write interface
│   ├── prompts/        ← all prompt templates as .md files
├── server/
│   └── mcp.py          ← MCP server — the only harness-facing interface
├── tests/
└── journal/            ← Obsidian-compatible, one .md per day
```

## Code conventions

- Type annotations on all function signatures. Use `from __future__ import annotations`.
- Docstrings on all public functions: one-line summary + Args/Returns/Raises.
- No `print()` in library code — use `loguru.logger`.
- Max line length: 100 (configured in `pyproject.toml`).
- `polars` for ETL and large transforms; `pandas` when interfacing with
  yfinance and exchange-calendars.
- All DataFrames must have a timezone-aware UTC `DatetimeIndex`. Column names: `snake_case`.
- Assert schema explicitly on load — never silently accept unexpected columns.

## Naming and communication conventions

- Never use the words "AI", "artificial intelligence", "harness", or "codex"
  (any casing) in branch names, commit messages, pull request titles, or pull
  request descriptions.
- Avoid them anywhere else we write about the project (docs, code comments,
  changelogs, release notes, journal entries). Use neutral, concrete language
  instead — e.g. "ingest job", "fact store", "market-memory service".

## Data sources — free only

| Source | Library / mechanism | What it provides |
|---|---|---|
| Yahoo Finance | `yfinance` | OHLCV, adjusted prices (US + EU via exchange suffixes), vol indices (^VIX, ^VSTOXX) |
| Google News RSS | `feedparser` | Per-ticker news headlines, keyless |
| Yahoo Finance RSS | `feedparser` | Per-ticker financial press headlines |
| SEC EDGAR | `requests` | US filings: 8-K, 10-K, proxy, insider transactions |
| FRED | `requests` (direct REST) | Macro: rates, inflation, credit spreads, VIXCLS |
| ECB SDW | `requests` (REST) | EUR rates, FX, EA macro |
| Company newsroom RSS | `feedparser` | Primary sources where available |
| STOXX selection list | `requests` | Official STOXX 600 membership + internal keys (keyless CSV) |
| BlackRock fund API | `requests` | iShares CSPX S&P 500 holdings (keyless) |
| OpenFIGI | `requests` | Free ISIN/CUSIP mapping for universe enrichment |
| Yahoo search API | `requests` | ISIN/CUSIP/ticker → symbol resolution (keyless) |

NewsAPI, OpenBB, and arXiv/SSRN are **optional supplements only** — never the
backbone. NewsAPI's free tier (100 req/day, 24h delay) is too constrained to
depend on. If a feature requires data only available via a paid API, stub the
interface and note the dependency in a `# TODO(paid-data):` comment.

`yfinance` survivorship caveat: Yahoo Finance returns only currently-listed
tickers. Compensate by maintaining a manual delisted-ticker log in
`data/universe/delisted.csv` and including those tickers in historical fetches.

## Storage architecture

Price/macro/news data is stored as **Parquet / NDJSON** in `data/raw/`
(immutable after download). Derived series (e.g. realized volatility) live in
`data/processed/` as Parquet. DuckDB reads these files natively — no ETL needed.

Structured queryable data lives in **DuckDB** (`data/db/research.duckdb`):

| Table | Purpose |
|---|---|
| `articles` | Deduplication registry and ingest audit trail |
| `ticker_facts` | Versioned knowledge base — one row per extracted fact |
| `agent_runs` | Audit log for every job execution |
| `market_views` | Free-text market snapshots written by the harness |
| `market_view_structures` | Structured JSON derived from free-text views |

The schema is defined in `data/db/schema.sql` and is the single source of truth.
Never modify the database schema outside of that file.

News and fact embeddings live in **ChromaDB** (`data/db/vectors/`). Each row in
`ticker_facts` has a corresponding vector keyed by `embedding_id`. Neither
`research.duckdb` nor the ChromaDB vector store are committed to git.

## News ingest pipeline

Runs daily as a batch job. Steps in order:

1. **Fetch** — `data/sources/rss.py` + `data/sources/sec.py` pull articles for
   all universe tickers. Raw JSON is written to `data/raw/news/{date}.ndjson`
   (immutable after write).
2. **Level-1 dedup** — skip any article whose `url_hash` or `content_hash`
   already exists in `articles` table. Cheap, no LLM call.
3. **Pre-score** — `pre_score = source_credibility[source] × exp(-λ × days_old)`.
   `source_credibility` is a static dict in `agent/ingestion.py`. `λ` controls
   recency decay.
4. **Fact retrieval** — for each new article, fetch current facts for the ticker
   from DuckDB (point-in-time: `valid_until IS NULL`) and top-5 semantically
   similar facts from ChromaDB.
5. **LLM extraction** — prompt template in `agent/prompts/fact_extraction.md`.
   Agent responds with either `REDUNDANT` or new facts, each with a `confidence`
   score (LLM adjusts pre_score based on language hedging).
6. **Write** — new facts inserted into `ticker_facts`; superseded facts get
   `valid_until`; statements embedded in ChromaDB.
7. **Audit** — run metadata written to `agent_runs`.

## Fact store conventions

Facts are **versioned, never mutated**. When a fact changes (e.g. CEO
replacement), set `valid_until` on the old row and insert a new one. This
preserves full history and enables look-ahead-safe point-in-time queries.

**Always query with a point-in-time filter:**
```sql
SELECT statement FROM ticker_facts
WHERE ticker = :ticker
  AND valid_from <= :as_of
  AND (valid_until IS NULL OR valid_until > :as_of);
```

Querying without `:as_of` is a look-ahead bias violation. The
`get_current_facts()` function in `agent/knowledge.py` enforces this — use it,
never raw SQL in research code.

`fact_type` is a free-form string assigned by the LLM (e.g. `'guidance'`,
`'management'`, `'macro_exposure'`, `'risk'`, `'catalyst'`). No fixed taxonomy
for now.

## Market views conventions

Market views are the core product: historical, point-in-time market snapshots
written incrementally by the harness.

- **Free text is immutable.** `view_text` is never edited in place; a new view
  supersedes contextually, it does not rewrite history.
- Every view records `as_of` (the market date it refers to), `scope`
  (`market`/`ticker`/`theme`), the writing model, and `data_cutoff`.
- Views optionally record `used_fact_ids` — the `ticker_facts` ids the harness
  used when writing the view (write-time provenance, no FK).
- **Structured data is derived, never authoritative.** The structuring job reads
  free-text views and writes JSON into `market_view_structures`, keyed by
  `schema_version`. Free text always remains available.
- Views are queried as-of: `as_of <= :date`. Newer views do not retroactively
  change what the harness believed at an earlier date.

## MCP server conventions

- `server/mcp.py` is the **only** harness-facing interface. No other API surface.
- Read tools: `get_prices`, `get_volatility`, `get_news`, `get_facts`,
  `get_market_views`, `search_news`. Write tools: `save_market_view`,
  `save_fact`. Job tool: `run_ingest`.
- Every tool has a test in `tests/` using an in-memory DuckDB. No live calls in
  tests.
- Tool docstrings must state the argument schema and the returned rows — the
  harness sees these descriptions, not the code.

## Agent conventions

- All prompt templates live in `agent/prompts/*.md` — never hardcode prompts in
  Python strings.
- LLM calls wrapped with `tenacity` retry and explicit timeout.
- Model: `claude-sonnet-4-5` (configurable via `LLM_MODEL` env var). Document in
  a comment if deviating.
- Every agent tool has a corresponding test in `tests/test_agent_tools.py` using
  mocked responses. No live API calls in tests.

## Testing requirements

- `agent/tools.py` and `server/mcp.py`: 100% coverage with mocked API calls.
- `agent/knowledge.py`: test `get_current_facts()` with point-in-time boundary cases.
- `data/universe/loader.py`: point-in-time filtering with known add/remove dates.
- `agent/views.py`: write/read round-trip, as-of filtering, idempotent structuring.
- `data/processed/volatility.py`: known return series → known realized vol output.
- Full test suite runs in <60 seconds with no network access. Use fixtures for
  all market data. Use an in-memory DuckDB instance (`:memory:`) in all
  DB-touching tests.

## Env vars (load via python-dotenv, never commit .env)

```
ANTHROPIC_API_KEY
LLM_MODEL               # default: claude-sonnet-4-5
FRED_API_KEY
TELEGRAM_BOT_TOKEN      # owned by the harness, not marketmind
TELEGRAM_CHAT_ID
```

## Hard constraints — do not override without explicit instruction

- Historical data only. No live feeds, no paper trading, no execution layer.
- No paid data sources. See data sources table.
- No options or derivatives.
- No modifications to `data/raw/` after initial download.
- MCP is the only harness-facing interface. No HTTP API, no UI, no CLI surface
  beyond the job entry points above.
- `agent/knowledge.py` functions must always accept and enforce an `as_of` date parameter.
- DB schema changes go through `data/db/schema.sql` only — never ALTER TABLE in
  application code.
- Git: never commit `.env`, raw data files, `research.duckdb`, ChromaDB vectors,
  or notebooks with embedded outputs.
