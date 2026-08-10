# MCP ingest control — design

Date: 2026-08-09
Status: approved, ready for planning
Base: `main` @ 56ca3c7 (includes PR #22, which added `IngestAborted`)

## Problem

`run_ingest` in `server/mcp.py` has never worked. It calls
`run_daily_ingest(date)` with no `llm` argument, and `run_daily_ingest`
raises `ValueError("llm callable is required")` in its first few lines. Every
invocation fails. The tool's docstring compounds this by claiming it raises
`NotImplementedError`, which is wrong in both directions.

The failure is masked by `tests/test_mcp_tools.py::test_run_ingest_tool`, which
monkeypatches `run_daily_ingest` with a two-line fake. The test asserts the
happy path against a stub of the only thing that fails.

This matters because `server/mcp.py` is the only external interface to this
service. A broken job tool means the ingest pipeline cannot be triggered on
demand at all — only by the systemd timer.

`todo.md` T6 additionally asks for `profiles_only` / `backfill` flags on
`run_ingest`, a `run_filings_refresh` tool, and a `get_ingest_status` tool.
All of it is in scope here.

## Constraints

- `uv run` for everything.
- Tests offline, no live API calls, in-memory or temp-file DuckDB.
- Full suite under 60s.
- CI enforces ruff plus `pytest --cov-fail-under=85`.
- DB schema changes go through `data/db/schema.sql` only. This design needs
  none: `agent_runs` already carries `job_type` and the `filings_*` columns.

## Decisions

A full ingest walks ~1,100 tickers with a model call per article — hours of
wall time. A synchronous tool call would block the caller for the whole run and
time out. Three decisions follow from that:

1. **Runs execute in the background.** `run_ingest` returns a `run_id`
   immediately; progress is polled through `get_ingest_status`.
2. **One run at a time, globally.** A second call is refused with a message
   naming the in-flight run. This avoids duplicated model spend and DuckDB
   write contention, including against the systemd timer's own run.
3. **Ticker subsetting.** `run_ingest` accepts an optional `tickers` list so a
   small, targeted run is possible. No pipeline change is needed for this —
   both `run_daily_ingest` and `run_daily_fetch` already accept a `universe`
   frame.

## Architecture

### New module: `server/jobs.py`

`server/mcp.py` is ~800 lines of query tools. Background-run bookkeeping is a
separate concern with its own tests, so it gets its own module.

```
RunRecord              run_id, job_type, state, started_at, finished_at,
                       tickers_done, tickers_total, counts, error, thread
RunInProgress          raised when the single run slot is taken
start_run(job_type, work) -> str    claim slot, spawn daemon thread, return run_id
_execute_run(record, work) -> None  synchronous body — unit-testable, no threads
current_run() -> RunRecord | None   most recent run, running or finished
```

`state` is one of `running` | `succeeded` | `failed`. The slot guard tests
`state == "running"`, so a finished record stays readable for polling.

`work` is a `Callable[[ProgressCallback], dict[str, int]]`. `jobs.py` never
imports the pipeline — it runs a closure supplied by the tool layer. That keeps
it testable with trivial fakes and keeps the pipeline unaware of threading.

`_execute_run` sets `state` and `counts` on success, and on failure records
`error` (`IngestAborted` message verbatim, otherwise `"<Type>: <msg>"`) and
logs via `logger.exception`.

### Pipeline change: progress hook

`run_daily_ingest` and `run_daily_fetch` each gain one optional keyword:

```python
progress: ProgressCallback | None = None
```

`ProgressCallback = Callable[[int, int, dict[str, int]], None]`, invoked after
each ticker completes with `(done, total, running_summary)`. Purely additive;
existing callers are unaffected. Without it there is no way to report live
progress, because the `agent_runs` row is only written when a run ends.

### Tool surface in `server/mcp.py`

```python
run_ingest(date: str, profiles_only: bool = False,
           backfill: bool = False, tickers: list[str] | None = None) -> str
run_filings_refresh(run_date: str = "") -> str      # "" means today
get_ingest_status(limit: int = 5) -> dict[str, object]
```

A private `_universe_for(date, tickers)` helper loads `get_universe(date)` and
filters it to the requested tickers.

`run_ingest` returns e.g.
`"Ingest started for 2026-08-09 (run_id=ab12cd…, 1103 tickers). Poll get_ingest_status()."`

Tool docstrings state the argument schema and the returned shape — the calling
agent reads those descriptions, not the code. The false
`Raises: NotImplementedError` on `run_ingest` is removed.

### Database access

The background thread opens its own connection via `init_db(DB_PATH)` inside
the thread; DuckDB connections must not be shared across threads. For news the
connection is passed explicitly as `conn=`; `run_daily_fetch` already requires
one. `get_ingest_status` reads through the existing `_connect()` from the main
thread — DuckDB caches the database instance per path within a process, so a
concurrent read against an in-flight write is safe.

## Error handling

Two classes, deliberately separated.

**Synchronous refusals** — validated in the calling thread before any thread is
spawned, so the caller gets an immediate diagnostic string rather than a
`run_id` that dies silently:

| Condition | Response |
|---|---|
| Malformed `date` | `Invalid date 'foo': expected YYYY-MM-DD.` |
| No universe match for `tickers` | `No universe entries on 2026-08-09 for: ZZZZ.` |
| Partial ticker match | Proceeds on the matches; names the dropped ones in the return string |
| `ChatClient()` raises `LLMError` | `Cannot start ingest: no API key: set AI_API_KEY or LLM_API_KEY.` |
| Run already in flight | `Run ab12cd… (news) already in flight since <ts>; refusing to start a second.` |

**Asynchronous failures** — recorded on the run record, surfaced by polling.
`IngestAborted` (the consecutive-failure circuit breaker) sets
`state="failed"` with the breaker message. `run_daily_ingest` already writes
its own `agent_runs` row with `error` populated before re-raising, so the audit
trail stays intact. Any other exception sets `state="failed"` with a typed
message.

## `get_ingest_status` return shape

```python
{
  "run": {                      # most recent in-process run, or None
    "run_id": str, "job_type": str, "state": str,
    "started_at": str, "finished_at": str | None,
    "elapsed_seconds": float,
    "tickers_done": int, "tickers_total": int,
    "counts": dict[str, int], "error": str | None,
  },
  "history": [ ... ],           # agent_runs rows, newest first, `limit` of them
}
```

While a run is live, `counts` holds the running summary most recently passed
to the progress callback; once it ends, `counts` holds the pipeline's final
return value. `finished_at` is `None` and `elapsed_seconds` is measured from
`started_at` to now while `state == "running"`.

`history` rows come from `agent_runs` via the existing `_rows_to_dicts`, so
runs started by the systemd timer appear there too, not only runs started
through this interface.

## Testing

The governing requirement: the missing-argument class of bug must not recur.

**New `tests/test_jobs.py`** — covers `server/jobs.py` in isolation with
trivial closures: happy path, slot guard raises `RunInProgress`, failure path
records the error, progress callback updates `tickers_done`. Uses
`_execute_run` directly for determinism, plus one real-thread test that joins.

**`tests/test_mcp_tools.py`** — `test_run_ingest_tool` is deleted, not
amended. Its replacement drives the real `run_daily_ingest` end to end:

- `mcp_mod.DB_PATH` → a `tmp_path` database
- `mcp_mod.get_universe` → a small two-row polars frame
- `agent.ingestion.fetch_company_news` / `fetch_company_description` →
  fixtures
- a fake `llm` callable that dispatches on prompt content to answer both the
  profile prompt and the extraction prompt

The test joins the run thread, then asserts facts landed in `ticker_facts`, an
`agent_runs` row exists, and `record.error is None`. That last assertion is
what fails loudly if anyone drops `llm=` again.

Additional cases:

- `IngestAborted` path: fake `llm` always raises, `MAX_CONSECUTIVE_FAILURES`
  patched to 2; status reports `state="failed"` with the breaker message.
- Each synchronous refusal in the table above.
- `run_filings_refresh` against a fake `EdgardClient` whose `submissions()`
  yields nothing — real `run_daily_fetch`, real DuckDB, zero network, and an
  `agent_runs` row with `job_type='filings'`.

No `sleep` calls; every spawned thread is joined. All offline, well inside the
60s budget. New code is covered, keeping the suite above the 85% gate.

## Out of scope

- `ingest_checkpoints` and resume-from-checkpoint (T2). The table does not
  exist in `data/db/schema.sql`; only `fetch_checkpoints`, which serves
  filings. `get_ingest_status` reads `agent_runs` alone.
- Prefect orchestration (a separate decision recorded in `todo.md`).
- T7 documentation of the full pipeline DAG. This branch updates only the MCP
  tool list in `README.md`.
