# marketmind — Project Roadmap

## Mission

marketmind is a market-memory service for Hermes. It stores historical prices,
macro observations, news, filings, versioned facts, and immutable market views,
then exposes them through MCP. It has no UI or public API.

## Current architecture

```
Hermes
  │ MCP (authenticated streamable HTTP on localhost)
  ▼
marketmind
  ├── scheduled jobs    universe · prices · macro · filings · news · views
  ├── MCP server        read/write tools and job controls
  └── storage
      ├── PostgreSQL    all structured and queryable state, including vectors
      ├── Parquet       immutable price/macro archives and derived series
      └── NDJSON        immutable raw news archives
```

The fresh PostgreSQL schema is `data/db/schema_postgres.sql`. Existing databases
advance through checksum-verified files in `data/db/postgres_migrations/`.

## Completed foundation

- [x] Point-in-time company identifiers and provider listings in PostgreSQL.
- [x] S&P 500 and STOXX Europe 600 refresh with append-only membership history.
- [x] Incremental prices, FX, volatility indices, macro data, dividends, and
      short interest persisted in PostgreSQL with immutable filesystem archives.
- [x] RSS/news ingestion, deduplication, versioned facts, and pgvector retrieval.
- [x] Immutable market views and versioned structured representations.
- [x] SEC filings, normalized filing text, insider ownership, 13F holdings, and
      European filing metadata in PostgreSQL.
- [x] Authenticated MCP server and lock-protected background refresh controls.
- [x] Docker Compose deployment, schema migration ledger, filesystem backup,
      PostgreSQL dump, and offline test suite.
- [x] PostgreSQL-only cutover; no secondary embedded database.

## Remaining work

- [ ] Maintain a reviewed delisted-ticker history and include it in backfills.
- [ ] Complete ISIN/CUSIP enrichment coverage for both universes.
- [ ] Expand company newsroom feed coverage and SEC form coverage.
- [ ] Add operational dashboards from `agent_runs` and persisted refresh state.
- [ ] Complete a 30-day unattended production run and document recovery drills.

## Definition of production-ready

Two consecutive daily cycles append cleanly, every job records a terminal audit
status, point-in-time queries pass boundary tests, backups restore successfully,
and the MCP client can read and write through the authenticated local endpoint.
