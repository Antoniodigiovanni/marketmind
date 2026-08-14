# Postgres App Port — Ingestion Pipeline Cutover (Sub-phase B) — Design

**Date:** 2026-08-14
**Status:** Approved for planning
**Scope note:** Second half of the facts/`agent/knowledge.py` cutover flagged
in `2026-08-13-postgres-mcp-facts-cutover-design.md` §6 as the largest,
highest-risk remaining piece. Sub-phase A (merged) cut `get_facts`,
`save_fact`, and `get_company_profile` over directly to the Postgres repos
without touching the ingestion pipeline. This phase rewires the pipeline
itself — the one place that touches facts, profiles, aggregates, articles,
and runs together.

## 1. Company resolution

Every per-company step in the pipeline needs a `company_id`. The lookup is:

```python
company_id = await resolve_identifier(
    conn, id_type="ticker", id_value=ticker, as_of=date_as_date
)
if company_id is None:
    company_id = await get_or_create_identifier(
        conn, id_type="ticker", id_value=ticker, valid_from=date_as_date,
        source="ingest", name=name, country=country, sector=sector,
    )
```

`resolve_identifier(as_of=...)` is always tried first — this is the fix the
immediately-preceding PR applied to `get_facts`/`save_fact`/
`get_company_profile`, extended here. `get_or_create_identifier` is only a
fallback for a ticker with no covering identifier window at all (first time
ever seen, or a genuine historical gap); it is never the primary lookup.

`get_or_create_identifier` internally resolves via `get_current_company_id`
(no `as_of`), which does not detect ticker reuse — its own docstring
documents this and defers it to separate follow-up work, the same
limitation `data/db/universe.py::sync_universe` already accepts today. This
phase inherits that pre-existing, project-wide gap rather than solving it;
it does not reintroduce the specific bug (skipping `resolve_identifier`
entirely) that was fixed in the prior PR.

Company attribute refresh keeps today's lazy-write behavior: description,
and the name/country/sector refresh that piggybacks on it, are only written
the first time a company's cached description is empty — not every run.

```python
company = await get_company(conn, company_id=company_id)
description = company["description"] if company else None
if not description:
    try:
        description = description_fetcher(name)
    except Exception as exc:
        logger.warning(f"description fetch failed for {ticker}: {exc}")
        description = ""
    await update_company_description(
        conn, company_id=company_id, description=description,
        description_source="wikipedia" if description else None,
    )
```

## 2. `agent/ingestion.py` async rewrite

`run_daily_ingest` becomes `async def`, taking `conn: psycopg.AsyncConnection`
instead of a DuckDB connection, and drops the `vector_store` parameter
entirely — `data/db/facts.py`/`aggregate_facts.py` always embed via
`agent/embedding.py::hash_embed`, so the old optional-vector-store /
lexical-Jaccard-fallback branch in `search_similar_facts` has no Postgres
equivalent and is not ported.

A `run_daily_ingest_with_pool(date, ...)` wrapper opens its own pool, checks
out one connection, and delegates:

```python
async def run_daily_ingest_with_pool(date: str, **kwargs) -> dict[str, int]:
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            return await run_daily_ingest(conn, date, **kwargs)
    finally:
        await close_pool(pool)
```

This mirrors `data/filings/fetch.py`'s already-shipped
`run_daily_fetch`/`run_daily_fetch_with_pool` split, including the reason:
`server/jobs.py` runs work on a plain background thread (not the MCP
server's event loop), so it cannot share the server's `_pg_pool()` and must
open a fresh pool inside its own `asyncio.run()` call.

Per-step replacement of `agent/knowledge.py` calls:

| Old (`agent/knowledge.py`, DuckDB) | New (`data/db/*.py`, Postgres) |
|---|---|
| `upsert_company` | §1 above (`resolve_identifier` / `get_or_create_identifier` / `update_company_description`) |
| `get_current_company_profile` | `company_profiles.get_current_profile(company_id=..., as_of=...)` |
| `write_company_profile` | `company_profiles.create_profile(company_id=..., ...)` |
| `get_current_facts` | `facts.get_current_facts(company_id=..., as_of=...)` |
| `search_similar_facts` | `facts.search_similar_facts(company_id=..., embed=hash_embed, ...)` — always embedding-backed now |
| `write_fact` | `facts.create_fact(company_id=..., embed=hash_embed, model="hash-ngram", ...)` |
| `write_aggregate_fact` | `aggregate_facts.create_aggregate_fact(embed=hash_embed, model="hash-ngram", ...)` |
| `get_current_aggregate_facts` | `aggregate_facts.get_current_aggregate_facts` (same name, same shape) |
| local `register_article` | `data/db/articles.py::register_article(company_id=..., ...)` |
| local `_set_article_status` | `data/db/articles.py::set_article_status(company_id=..., ...)` |
| local `_write_agent_run` | `data/db/runs.py::start_run` / `finish_run` (§3) |

Row-shape changes that ripple into `build_extraction_messages`/`apply_facts`:
current/similar facts now carry `fact_id` (not `id`), and
`search_similar_facts` returns `similarity` (not `score`). The
supersession-matching map in `apply_facts` (`statement -> id`) becomes
`statement -> fact_id`. `evidence` passed to `create_aggregate_fact` is
`list[uuid.UUID]` directly (no `str()` — the old DuckDB code stringified
because DuckDB stored ids as hex strings; Postgres ids are already UUIDs
end to end).

`RawNewsArchive` (the per-date NDJSON archive under `data/raw/news/`) is
unchanged — it is filesystem-based and has no DuckDB coupling.
`pre_score`, `build_extraction_messages`, `build_aggregate_messages`,
`parse_fact_list_response`, `_default_profile_builder`, `_load_prompt` are
pure functions and are unchanged. The dead, already-unused `_sha256` helper
is dropped since the file is being rewritten anyway.

Article-company dedup semantics change slightly, matching
`data/db/articles.py`'s documented design: dedup is now per
`(article, company)` pair via `article_companies`, not purely global. The
same story surfacing in two different companies' RSS feeds is processed
once per company rather than being dropped for the second company. This is
`articles.py`'s existing, already-tested behavior — the ingestion cutover
just starts exercising it.

## 3. Run audit

`start_run(conn, run_date=date_as_date, job_type="news")` is called once,
before the per-company loop begins — writing the row immediately (not only
at the end) means a hard crash mid-run still leaves a `'started'` row
instead of nothing, per `runs.py`'s own documented intent.

```python
run_id = await start_run(conn, run_date=date_as_date, job_type="news")
try:
    ... existing per-company loop + aggregation ...
except IngestAborted as exc:
    await finish_run(conn, run_id=run_id, status="aborted",
                      counters=summary, error=str(exc))
    raise
except Exception as exc:
    await finish_run(conn, run_id=run_id, status="failed",
                      counters=summary, error=str(exc))
    raise
else:
    await finish_run(conn, run_id=run_id, status="succeeded", counters=summary)
```

The bare `except Exception` branch is new relative to the DuckDB pipeline,
which only ever wrote an audit row for `IngestAborted` — any other
unhandled exception left zero trace in `agent_runs` (only `server/jobs.py`'s
in-memory `RunRecord` captured it, and only for the life of the process).
Wiring `finish_run` for the general case is a deliberate behavior
improvement enabled by `runs.py`'s `status` column (DuckDB's fixed-column
`agent_runs` had no equivalent), not scope creep — it is what "wiring in"
`start_run`/`finish_run` means to do properly.

`summary` keys are unchanged (`articles_fetched`, `articles_skipped`,
`articles_redundant`, `articles_errored`, `facts_written`,
`facts_superseded`, `aggregate_written`, `profiles_built`,
`profiles_failed`) and are stored as-is in `counters` JSONB — no
per-job-type fixed-column mapping needed anymore.

No single transaction wraps the whole run. Each repository call
(`create_fact`, `register_article`, `create_profile`, ...) commits
independently via its own `async with conn.transaction():`, exactly as
today. Wrapping the entire run in one outer transaction would silently
defeat the resumability `start_run`/article-dedup are meant to provide — a
crash three hours in would roll back three hours of committed facts, not
just the in-flight write.

## 4. `agent/knowledge.py` deletion

Deleted once `agent/ingestion.py` no longer imports it (nothing else does —
confirmed by search; only `agent/ingestion.py` and its own tests import it
today).

## 5. `agent/embeddings.py` / ChromaDB removal

`agent/embeddings.py` (`ChromaVectorStore`, `HashEmbeddingFunction`) is
deleted. `server/mcp.py::_build_vector_store` (its only remaining caller,
already unused by the Sub-phase A tools) is deleted. The `chromadb`
dependency is dropped from `pyproject.toml`. `agent/embedding.py::hash_embed`
(shipped in the Sub-phase A PR specifically for this cutover) is the sole
replacement and needs no changes.

## 6. `server/mcp.py` tool cutover

`run_ingest`'s `work()` closure switches from building a DuckDB connection
and calling the sync `run_daily_ingest` to:

```python
def work(progress: jobs.ProgressCallback) -> dict[str, int]:
    return asyncio.run(
        run_daily_ingest_with_pool(
            date, universe=universe, llm=llm,
            profile_builder=_default_profile_builder(llm),
            backfill=backfill, profiles_only=profiles_only, progress=progress,
        )
    )
```

`_build_vector_store()` is no longer called from `run_ingest` (dropped
along with the `vector_store` parameter).

`get_ingest_status` becomes `async def` and switches its `history` source:

```python
@mcp.tool()
async def get_ingest_status(limit: int = 5) -> dict[str, object]:
    record = jobs.current_run()
    pool = await _pg_pool()
    async with pool.connection() as conn:
        rows = await list_recent_runs(conn, limit=limit)
    return {
        "run": record.snapshot() if record else None,
        "history": [_jsonify_pg_row(row) for row in rows],
    }
```

This is a visible MCP-facing shape change, called out explicitly in the
docstring per the standard the views/filings PRs already held themselves
to: `history` rows now carry a nested `counters` dict instead of flattened
`articles_fetched`/`facts_written`/etc. keys, and the `id` key is now
`run_id`.

**Known, accepted gap:** `run_filings_refresh` still logs to DuckDB's
`agent_runs` only (`data/filings/store.py::log_run`, untouched — filings'
own Postgres audit-log cutover is separate, later work per
`docs/superpowers/plans/2026-08-11-postgres-filings-cutover.md`). Once
`get_ingest_status.history` reads exclusively from Postgres, filings runs
stop appearing in it until that follow-up lands. This was already flagged
as expected in the Sub-phase A design doc §3 and is not addressed here.

`AGENT_RUN_COLUMNS` becomes dead once `get_ingest_status` no longer builds
its query from it, and is deleted. `_connect()`, `DB_PATH`, `init_db` stay
in `server/mcp.py` — `run_filings_refresh`'s closure still needs a DuckDB
connection for the filings dual-write.

## 7. Data-source dedup / checkpointing

Article-level dedup and resumability (the actual mechanism the DuckDB
pipeline had: a rerun of the same date skips articles already registered)
is fully covered by `data/db/articles.py::register_article` — no gap.

`schema_postgres.sql` also defines an `ingest_checkpoints` table
(per-company, per-stage `'profile'|'fetch'|'extract'` resumability) with no
DuckDB precedent and no repository module. This is forward-looking schema,
not something this phase's DuckDB source pipeline ever implemented or
needs to port. Out of scope for this phase; a future phase can build the
repository module and wire it in if finer-grained (sub-article) resumability
is ever needed.

## 8. Testing

Every Postgres-touching test uses the `pg_conn` fixture against a real
local/CI Postgres instance, per CLAUDE.md's established exception.

- `agent/ingestion.py`: rewrite `tests/test_ingestion.py` to call
  `run_daily_ingest(pg_conn, date, ...)` directly (mirroring how
  `run_daily_fetch` is tested), asserting through the Postgres repo
  functions (`facts.get_current_facts`, `aggregate_facts.get_current_aggregate_facts`,
  `runs.list_recent_runs`) instead of raw DuckDB SQL. Companies/tickers are
  seeded via `get_or_create_identifier`, matching the pattern already used
  in `tests/test_mcp_facts_pg.py`.
- `server/mcp.py`: rewrite the `run_ingest`/`get_ingest_status` tests in
  `tests/test_mcp_jobs.py` to add the `postgres_pool` fixture dependency
  (same reasoning `test_run_filings_refresh_*` already documents: the
  background thread opens its own pool against the same test DSN, so the
  test only needs to guarantee the schema exists first) and to assert
  against Postgres rows instead of DuckDB `agent_runs`. The `_offline`
  fixture drops its `_build_vector_store` stub (function no longer exists).
  `test_build_vector_store_returns_a_fact_store` is deleted.
  `test_run_filings_refresh_*` tests are unchanged (filings pipeline is
  untouched by this phase).
- `agent/knowledge.py`'s and `agent/embeddings.py`'s dedicated test files
  (`tests/test_knowledge.py`, `tests/test_embeddings.py`,
  `tests/test_agent_embedding.py` — the last one covers `agent/embedding.py`,
  kept) are deleted along with the modules they test.

## 9. Explicitly out of scope

- `data/db/universe.py::sync_universe` wiring (still not called from
  anywhere; ingestion creates companies on demand via `get_or_create_identifier`
  exactly as the DuckDB pipeline did, without depending on a separate sync
  step having run first).
- `run_filings_refresh` / filings audit-log cutover to Postgres.
- `ingest_checkpoints` repository module (§7).
- Any change to `data/universe/loader.py::get_universe` (CSV-backed,
  untouched — out of this migration's scope per CLAUDE.md).
