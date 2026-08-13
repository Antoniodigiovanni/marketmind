# Postgres market-views cutover — design

Date: 2026-08-11
Status: approved, ready for planning
Base: `main` @ 8418f43 (PR #32, aggregate-facts repo)

## Problem

`agent/views.py` (194 lines) is the last piece of the market-views feature
still running against DuckDB: `save_market_view`, `get_market_views`,
`structure_views`. `server/mcp.py` exposes the first two as MCP tools
(`get_market_views`, `save_market_view_tool`). Five prior PRs landed
Postgres repository modules additively (`data/db/companies.py`,
`identifiers.py`, `listings.py`, `universe.py`, `facts.py`,
`company_profiles.py`, `aggregate_facts.py`) without wiring any of them
into a real caller. This is the first PR that both adds a repository
module (`data/db/views.py`) *and* cuts a live caller over — `agent/views.py`
and the two MCP tools move onto it.

Three other pieces of the same migration wave are running in parallel:
filings, market data, and a facts/company_profiles/aggregate_facts cutover
that deletes ChromaDB. None of those are touched here.

## Constraints

- `uv run` for everything.
- `data/db/schema_postgres.sql` is the migration-target schema; additive
  changes to it are cheap and precedented (`listings.status`,
  `aggregate_fact_embeddings`).
- Repo functions in `data/db/` take `conn: psycopg.AsyncConnection`
  explicitly, scope their own SQL in `async with conn.transaction():`, never
  call `commit()`/`rollback()` directly.
- Point-in-time reads take `as_of: dt.date` as a mandatory keyword-only
  parameter.
- No SQL outside `data/db/`.
- Do not touch `agent/knowledge.py`, `agent/embeddings.py`,
  `data/filings/*`, `data/fetch.py`, `data/dividends/*`,
  `data/short_interest/*`, `data/macro/*`, `server/jobs.py`, or any MCP tool
  other than `get_market_views` / `save_market_view_tool`.
- No concurrency-limiting or connection-pool-lifecycle work in `server/mcp.py`
  — that is an explicitly separate, later phase. A plain per-call connection
  (mirroring the existing DuckDB `_connect()` pattern) is acceptable for now.

## Decisions

### 1. Schema: add `market_views.used_fact_ids UUID[]`

The DuckDB `market_views.used_fact_ids` column (provenance — which
`ticker_facts`/`company_facts` rows informed a view) has no equivalent in
`schema_postgres.sql`. This is a real capability gap: dropping it silently
would regress auditability of what evidence backed a market view, and this
codebase's established response to an equivalent gap (aggregate facts had
no vector table at all) was to close it, not paper over it
(`aggregate_fact_embeddings`, PR #32).

Decision: add `used_fact_ids UUID[]` to `market_views` in
`schema_postgres.sql`, referencing `company_facts.fact_id` values by
convention (not a DB-enforced FK — Postgres doesn't support array foreign
keys, and the DuckDB original never enforced this either). Table count is
unchanged (31); `tests/test_postgres_schema.py` needs no update for this.
The running schema-history comment at the top of the file gets one more
entry, matching the `listings.status` / `aggregate_fact_embeddings` style.

### 2. Ticker → company_id resolution boundary

`market_views.company_id UUID` replaces `ticker TEXT`. `data/db/views.py`
takes `company_id: uuid.UUID | None` directly and contains no resolution
logic, matching every other repo module (`facts.py`, `aggregate_facts.py`
take `company_id`, not `ticker`). `agent/views.py` becomes the thin
orchestration layer: it keeps the scope/ticker/theme cross-field validation
it already has (this is business-rule validation, not SQL, so it belongs
above the repo layer, same as before), and adds one step — when
`scope == "ticker"`, resolve `ticker` to `company_id` via
`data/db/identifiers.py::resolve_identifier(conn, id_type="ticker",
id_value=ticker, as_of=as_of)` before calling `data/db/views.py`.

Write path (`save_market_view`): an unresolvable ticker is a hard error
(`ValueError`) — a ticker-scoped view with no company to attach to would be
silently orphaned data.

Read path (`get_market_views`): an unresolvable ticker filter returns `[]`
rather than raising. A read is a filter, not an assertion that the ticker
must exist; a typo or a ticker that predates the identity model's coverage
should behave like "no matches," consistent with how every other filter
argument (`scope`) behaves on a miss.

The repository layer never sees a `ticker` string — this mirrors
`data/db/universe.py::sync_universe`, which delegates ticker resolution to
`identifiers.py` rather than reimplementing it, just one layer up (agent →
identifiers, instead of repo → identifiers) since `agent/views.py` is the
only caller-facing surface that still speaks "ticker."

Read-side output uses `company_id` (raw UUID), not a resolved ticker
string. Reconstructing "the ticker as of that view's date" would need a
reverse point-in-time identifier lookup that doesn't exist yet
(`identifiers.py` only resolves forward, ticker → company). Building that
is out of scope for a views cutover — it's identifier-layer work. Callers
that need a ticker can resolve `company_id` themselves via a future
identifiers.py addition; this PR does not add one speculatively.

### 3. `structure_views` stays a repo-owned, callable-parameterized write

Precedent: `data/db/facts.py::create_fact` takes an `embed: Callable[...]`
and does the embedding-plus-write inside its own transaction — cross-cutting
work injected as a callable lives in the repo module, not a separate agent
wrapper. `structure_views` has the same shape (`extractor: Callable` in,
row written out), so it moves into `data/db/views.py` directly rather than
being split across two layers. `agent/views.py` does not wrap it — callers
(currently just tests, and eventually a future job) call
`data.db.views.structure_views` directly. The default extractor stays a
stub raising `NotImplementedError` — this is pre-existing, unfinished LLM
wiring (roadmap item), not something this PR finishes.

The "skip if already structured" check keys off `(view_id, schema_version)`
— the new PK — not `view_id` alone, since the same view can now be
structured under multiple schema versions (the DuckDB version's known bug,
todo.md A1.9, that the new schema already fixes at the DDL level; the query
just needs to respect it). `schema_version` becomes `int` (was `str`), and
defaults to `1`.

### 4. `data_cutoff` becomes mandatory

`market_views.data_cutoff` is `DATE NOT NULL` in the new schema (was
optional `TIMESTAMPTZ` in DuckDB). `agent/views.py::save_market_view` and
`server/mcp.py`'s `save_market_view_tool` both drop the `| None` default —
`data_cutoff: str` becomes a required parameter with no fallback. There is
no live external caller of `save_market_view_tool` yet (marketmind has no
production deployment), so this is a clean break rather than a migration
concern.

### 5. Connection lifecycle in `server/mcp.py`

This is the first Postgres-backed tool wired into `server/mcp.py` (every
prior PR was additive-only). `market_views`/`market_view_structures` have
no `vector` columns, so the pgvector-registration machinery in
`data/db/pool.py` (`open_pool`, per-connection `configure`) is not needed
for correctness here, and standing up a long-lived pool plus its shutdown
hook inside `server/mcp.py` is exactly the "concurrency limits" scope this
task is told to leave alone.

Decision: add a small `_connect_pg()` helper in `server/mcp.py` that opens
one `psycopg.AsyncConnection` via `dsn_from_env()` (`data/db/pool.py`) per
tool call and closes it when the call finishes (`async with`), mirroring
the existing DuckDB `_connect()` shape (open per call, close per call).
This is a deliberately minimal bridge: whatever real pool-lifecycle
strategy `server/mcp.py` needs once more tools cut over is follow-up work,
not something to half-build here for two tools.

### 6. `async def` and the sync/async boundary

`agent/views.py`'s three functions and `data/db/views.py`'s functions all
become `async def` — they call `psycopg.AsyncConnection` and (for
`structure_views`) an `Extractor` that may itself be async. `server/mcp.py`
tool functions `get_market_views` and `save_market_view_tool` become
`async def`; FastMCP supports async tool handlers natively, so no
`asyncio.run()` bridge is needed at that call site. (The task brief allows
`asyncio.run()` for bridging a *synchronous* entry point if one were
needed — none is, here.)

## Architecture

```
data/db/views.py                         (new — all market_views SQL)
    save_market_view(conn, *, as_of, scope, company_id, theme, view_text,
                      model, data_cutoff, used_fact_ids=None) -> uuid.UUID
    get_market_views(conn, *, as_of, company_id=None, scope=None,
                      limit=50) -> list[dict]
    Extractor = Callable[[uuid.UUID, str, str], Awaitable[dict] | dict]
    structure_views(conn, *, extractor=None, schema_version=1,
                     extraction_model="claude-sonnet-4-5") -> int

agent/views.py                            (rewritten — thin orchestration)
    VALID_SCOPES unchanged
    save_market_view(conn, *, as_of: str, view_text, model, data_cutoff: str,
                      scope="market", ticker=None, theme=None,
                      used_fact_ids=None) -> str
        - cross-field validation (unchanged from today)
        - resolves ticker -> company_id via identifiers.resolve_identifier
          when scope == "ticker"; ValueError if unresolved
        - parses as_of / data_cutoff to dt.date
        - delegates to data.db.views.save_market_view
    get_market_views(conn, *, as_of: str, ticker=None, scope=None,
                      limit=50) -> list[dict]
        - resolves ticker -> company_id when given; [] on no match
        - delegates to data.db.views.get_market_views

server/mcp.py                             (rewired, two tools only)
    _connect_pg() -> psycopg.AsyncConnection   (new helper)
    async def get_market_views(...)             calls agent.views.get_market_views
    async def save_market_view_tool(...)        calls agent.views.save_market_view
```

`data/db/views.py`'s public dict shape for `get_market_views` rows:
`view_id, as_of, scope, company_id, theme, view_text, model, data_cutoff,
used_fact_ids, created_at` — raw Python types (`uuid.UUID`, `dt.date`,
`list[uuid.UUID] | None`). Note `model` here is a SELECT alias for the
`written_by` column, keeping the public field name stable across the
schema's internal rename; `view_id` (not `id`) matches the `fact_id` /
`aggregate_id` naming convention already used by `facts.py` /
`aggregate_facts.py`. No manual `isoformat()`/`str()` conversion is needed
at any layer — FastMCP serializes tool results through
`pydantic_core.to_json(..., fallback=str)`, which natively handles `date`
and `UUID`.

## Error handling

- `data/db/views.py::save_market_view` trusts its caller for cross-field
  validation (scope/ticker/theme consistency lives in `agent/views.py`) and
  the DB's own CHECK constraints (`view_scope_target`) as the last line of
  defense; it does not duplicate that validation.
- `agent/views.py::save_market_view` keeps today's `ValueError` messages for
  scope validation, and raises a new `ValueError` when a ticker fails to
  resolve to a company (`f"no company found for ticker {ticker!r} as of
  {as_of}"`).
- `data/db/views.py::structure_views` keeps raising `NotImplementedError`
  from the default extractor, matching today's stub behavior exactly.

## Testing

- `tests/test_db_views.py` (new): repo-layer tests against `pg_conn`,
  following `tests/test_db_facts.py`'s shape — insert via `create_company`,
  exercise `save_market_view`/`get_market_views` (as-of filtering, scope
  filtering, company_id filtering) and `structure_views` (idempotent skip
  keyed on `(view_id, schema_version)`, default stub raises).
- `tests/test_agent_views.py` (rewritten from whatever DuckDB test exists
  today): ticker resolution success/failure paths, cross-field validation
  unchanged, `data_cutoff` required.
- `tests/test_mcp_tools.py`: existing DuckDB-based tests for
  `get_market_views`/`save_market_view_tool` get replaced with
  `pg_conn`-backed async tests exercising the rewired tools end to end.
- Full suite stays green under
  `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/ -q --cov=. --cov-report=term-missing --cov-fail-under=85`.
