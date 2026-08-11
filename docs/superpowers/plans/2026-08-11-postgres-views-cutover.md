# Postgres market-views cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `agent/views.py` (market views + structured extraction) off DuckDB onto Postgres, adding `data/db/views.py` as the repository layer and rewiring `server/mcp.py`'s `get_market_views` / `save_market_view_tool` to call it.

**Architecture:** `data/db/views.py` owns all `market_views`/`market_view_structures` SQL, taking `company_id: uuid.UUID | None` directly (never `ticker`). `agent/views.py` becomes a thin async orchestration layer: keeps scope/ticker/theme cross-field validation, resolves `ticker` → `company_id` via `data/db/identifiers.py::resolve_identifier`, and delegates the actual write/read to `data/db/views.py`. `server/mcp.py` gets a small `_connect_pg()` helper (open one connection per tool call, close it after — no pool lifecycle work) and its two tools become `async def`.

**Tech Stack:** Python 3.11+, psycopg3 (async), pytest + pytest-asyncio (`asyncio_mode = "auto"`), PostgreSQL 16 + pgvector (pgvector not used by this table, no vector columns here).

## Global Constraints

- `uv run` for everything — never bare `python`/`pip`.
- Every `data/db/` function takes `conn: psycopg.AsyncConnection` explicitly and scopes its own SQL in `async with conn.transaction():`; never call `conn.commit()`/`conn.rollback()` directly.
- Point-in-time reads take `as_of: dt.date` as a mandatory keyword-only parameter, never defaulted.
- `from __future__ import annotations` at the top of every module touched.
- Docstrings: one-line summary + Args/Returns/Raises. Type hints everywhere.
- No SQL outside `data/db/`.
- No AI/tool attribution anywhere: no "Generated with", no Co-Authored-By, no model names in commits, branch names, or PR text.
- Full suite must stay green: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/ -q --cov=. --cov-report=term-missing --cov-fail-under=85`.
- `uv run ruff check . && uv run ruff format --check .` must be clean before every commit.
- Do not touch `agent/knowledge.py`, `agent/embeddings.py`, `data/filings/*`, `data/fetch.py`, `data/dividends/*`, `data/short_interest/*`, `data/macro/*`, `server/jobs.py`, or any MCP tool other than `get_market_views`/`save_market_view_tool`.
- Local Postgres: `docker compose up -d` (pgvector/pgvector:pg16, port 5433) — already running as `marketmind-postgres-1` in this environment.

---

## Task 1: Add `market_views.used_fact_ids UUID[]` to the schema

**Files:**
- Modify: `data/db/schema_postgres.sql:1-9` (header comment), `:384-400` (`market_views` table)
- Test: `tests/test_postgres_schema.py` (verify no change needed — table count stays 31)

**Interfaces:**
- Produces: `market_views.used_fact_ids` column, type `UUID[]`, nullable, no DB-enforced FK (Postgres has no array foreign keys) — referenced by convention against `company_facts.fact_id`.

- [ ] **Step 1: Edit the `market_views` table definition**

In `data/db/schema_postgres.sql`, find:

```sql
CREATE TABLE IF NOT EXISTS market_views (
    view_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of       DATE        NOT NULL,     -- market date the view refers to
    scope       TEXT        NOT NULL
        CHECK (scope IN ('market','ticker','theme')),
    company_id  UUID        REFERENCES companies(company_id) ON DELETE SET NULL,
    theme       TEXT,
    view_text   TEXT        NOT NULL,     -- immutable, never edited in place
    written_by  TEXT        NOT NULL,     -- model id
    data_cutoff DATE        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT view_scope_target CHECK (
        (scope = 'ticker' AND company_id IS NOT NULL) OR
        (scope = 'theme'  AND theme IS NOT NULL) OR
        (scope = 'market')
    )
);
```

Replace with (adds `used_fact_ids` before `created_at`):

```sql
CREATE TABLE IF NOT EXISTS market_views (
    view_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of       DATE        NOT NULL,     -- market date the view refers to
    scope       TEXT        NOT NULL
        CHECK (scope IN ('market','ticker','theme')),
    company_id  UUID        REFERENCES companies(company_id) ON DELETE SET NULL,
    theme       TEXT,
    view_text   TEXT        NOT NULL,     -- immutable, never edited in place
    written_by  TEXT        NOT NULL,     -- model id
    data_cutoff DATE        NOT NULL,
    used_fact_ids UUID[],                 -- company_facts.fact_id provenance; not FK-enforced
                                           -- (Postgres has no array foreign keys)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT view_scope_target CHECK (
        (scope = 'ticker' AND company_id IS NOT NULL) OR
        (scope = 'theme'  AND theme IS NOT NULL) OR
        (scope = 'market')
    )
);
```

- [ ] **Step 2: Update the running schema-history comment**

Find:

```sql
-- (listings.status added 2026-08-10 for the identity-port phase: 19 check
-- constraints as of that change. aggregate_fact_embeddings added 2026-08-11
-- for the aggregate-facts-repo phase: 31 tables as of that change.
-- aggregate_facts.dimension gained a CHECK constraint the same day: 20
-- check constraints as of that change.)
```

Replace with:

```sql
-- (listings.status added 2026-08-10 for the identity-port phase: 19 check
-- constraints as of that change. aggregate_fact_embeddings added 2026-08-11
-- for the aggregate-facts-repo phase: 31 tables as of that change.
-- aggregate_facts.dimension gained a CHECK constraint the same day: 20
-- check constraints as of that change. market_views.used_fact_ids UUID[]
-- added 2026-08-11 for the views-cutover phase, restoring the DuckDB
-- provenance column the initial port dropped; table count unchanged.)
```

- [ ] **Step 3: Verify the schema still applies and the table count assertion holds**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_postgres_schema.py -v`
Expected: all pass, `test_schema_applies_and_creates_the_expected_objects` still asserts `tables[0] == 31`.

- [ ] **Step 4: Commit**

```bash
git add data/db/schema_postgres.sql
git commit -m "feat(db): add used_fact_ids provenance column to market_views"
```

---

## Task 2: `data/db/views.py` — `save_market_view` and `get_market_views`

**Files:**
- Create: `data/db/views.py`
- Test: `tests/test_db_views.py`

**Interfaces:**
- Consumes: nothing from earlier tasks except the new schema column from Task 1.
- Produces:
  - `save_market_view(conn, *, as_of: dt.date, scope: str, view_text: str, model: str, data_cutoff: dt.date, company_id: uuid.UUID | None = None, theme: str | None = None, used_fact_ids: list[uuid.UUID] | None = None) -> uuid.UUID`
  - `get_market_views(conn, *, as_of: dt.date, company_id: uuid.UUID | None = None, scope: str | None = None, limit: int = 50) -> list[dict[str, object]]` — each dict has keys `view_id, as_of, scope, company_id, theme, view_text, model, data_cutoff, used_fact_ids, created_at`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db_views.py`:

```python
"""Tests for the market-views repository -- writes and as-of reads."""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest

from data.db.companies import create_company
from data.db.views import get_market_views, save_market_view

pytestmark = pytest.mark.asyncio


async def _company(pg_conn, name: str = "TestCo") -> uuid.UUID:
    return await create_company(
        pg_conn, name=name, country="US", sector="Technology", first_seen=dt.date(2000, 1, 1)
    )


async def test_save_market_view_returns_a_uuid(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    assert isinstance(view_id, uuid.UUID)


async def test_get_market_views_returns_what_was_saved(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 5))
    assert len(views) == 1
    assert views[0]["view_id"] == view_id
    assert views[0]["view_text"] == "Rates path is the key swing factor for H2."
    assert views[0]["model"] == "hermes"
    assert views[0]["company_id"] is None
    assert views[0]["used_fact_ids"] is None


async def test_get_market_views_is_point_in_time(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="future-looking",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    before = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 3))
    assert before == []


async def test_get_market_views_orders_newest_first(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 7, 1),
        scope="market",
        view_text="old",
        model="hermes",
        data_cutoff=dt.date(2026, 7, 1),
    )
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="new",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    assert len(await get_market_views(pg_conn, as_of=dt.date(2026, 7, 15))) == 1
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 5))
    assert [v["view_text"] for v in views] == ["new", "old"]


async def test_save_market_view_with_used_fact_ids_round_trips(pg_conn) -> None:
    fact_ids = [uuid.uuid4(), uuid.uuid4()]
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
        used_fact_ids=fact_ids,
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 5))
    assert views[0]["view_id"] == view_id
    assert set(views[0]["used_fact_ids"]) == set(fact_ids)


async def test_save_market_view_ticker_scope_round_trips_company_id(pg_conn) -> None:
    company_id = await _company(pg_conn)
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="ticker",
        company_id=company_id,
        view_text="AAPL-specific view.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4), company_id=company_id)
    assert [v["view_id"] for v in views] == [view_id]


async def test_save_market_view_ticker_scope_without_company_id_violates_db_constraint(
    pg_conn,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        await save_market_view(
            pg_conn,
            as_of=dt.date(2026, 8, 4),
            scope="ticker",
            view_text="orphaned",
            model="hermes",
            data_cutoff=dt.date(2026, 8, 4),
        )


async def test_get_market_views_filters_by_scope(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="theme",
        theme="semiconductors",
        view_text="Semis cycle turning up.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Broad view.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4), scope="theme")
    assert len(views) == 1
    assert views[0]["theme"] == "semiconductors"


async def test_get_market_views_filters_by_company_id(pg_conn) -> None:
    company_a = await _company(pg_conn, "CompanyA")
    company_b = await _company(pg_conn, "CompanyB")
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="ticker",
        company_id=company_a,
        view_text="About A.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="ticker",
        company_id=company_b,
        view_text="About B.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4), company_id=company_a)
    assert [v["view_text"] for v in views] == ["About A."]


async def test_get_market_views_respects_limit(pg_conn) -> None:
    for i in range(3):
        await save_market_view(
            pg_conn,
            as_of=dt.date(2026, 8, 4),
            scope="market",
            view_text=f"view {i}",
            model="hermes",
            data_cutoff=dt.date(2026, 8, 4),
        )
    views = await get_market_views(pg_conn, as_of=dt.date(2026, 8, 4), limit=2)
    assert len(views) == 2
```

- [ ] **Step 2: Run tests to verify they fail with an import error**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_db_views.py -v`
Expected: FAIL / collection error — `ModuleNotFoundError: No module named 'data.db.views'` (or `ImportError`).

- [ ] **Step 3: Write `data/db/views.py`**

```python
"""Market views repository — free-text market snapshots and their structured extractions.

Takes `company_id` directly, never `ticker` — ticker resolution is not this
module's job (see `agent/views.py`, which resolves ticker -> company_id via
`data/db/identifiers.py` before calling into this module), matching every
other repository module here (`facts.py`, `aggregate_facts.py` take
`company_id`, not a human-facing identifier).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


async def save_market_view(
    conn: psycopg.AsyncConnection,
    *,
    as_of: dt.date,
    scope: str,
    view_text: str,
    model: str,
    data_cutoff: dt.date,
    company_id: uuid.UUID | None = None,
    theme: str | None = None,
    used_fact_ids: list[uuid.UUID] | None = None,
) -> uuid.UUID:
    """Insert a free-text market view (immutable — never edited in place).

    Args:
        conn: An open connection.
        as_of: Market date the view refers to.
        scope: 'market' | 'ticker' | 'theme'. Not validated here — the
            caller (`agent/views.py`) validates scope/company_id/theme
            consistency; the database's `view_scope_target` CHECK
            constraint is the last line of defense.
        view_text: Free-text view content.
        model: Model identifier that produced the view (stored as
            `written_by`).
        data_cutoff: Date of the newest data used to write the view.
        company_id: The view's subject when scope == 'ticker'.
        theme: Theme label when scope == 'theme'.
        used_fact_ids: company_facts ids that informed this view
            (provenance). Not a DB-enforced foreign key — Postgres has no
            array foreign keys — just a recorded list.

    Returns:
        The new view_id.
    """
    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO market_views "
                "(as_of, scope, company_id, theme, view_text, written_by, data_cutoff, "
                "used_fact_ids) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING view_id",
                (as_of, scope, company_id, theme, view_text, model, data_cutoff, used_fact_ids),
            )
        ).fetchone()
    return row[0]


async def get_market_views(
    conn: psycopg.AsyncConnection,
    *,
    as_of: dt.date,
    company_id: uuid.UUID | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return market views as of a date (as_of <= :as_of), newest first.

    Args:
        conn: An open connection.
        as_of: Views referring to this date or earlier are returned.
        company_id: Optional filter, scope == 'ticker' rows for this company.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        limit: Maximum number of views to return.

    Returns:
        One dict per view: view_id, as_of, scope, company_id, theme,
        view_text, model, data_cutoff, used_fact_ids, created_at. Ordered
        by as_of descending, then created_at descending.
    """
    clauses = ["as_of <= %s"]
    params: list[object] = [as_of]
    if company_id is not None:
        clauses.append("company_id = %s")
        params.append(company_id)
    if scope is not None:
        clauses.append("scope = %s")
        params.append(scope)
    params.append(limit)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT view_id, as_of, scope, company_id, theme, view_text,
                   written_by AS model, data_cutoff, used_fact_ids, created_at
            FROM market_views
            WHERE {" AND ".join(clauses)}
            ORDER BY as_of DESC, created_at DESC
            LIMIT %s
            """,
            params,
        )
        return await cur.fetchall()
```

(`structure_views` and its `Extractor` type are added in Task 3 — this step only needs the two functions above plus the shared imports; leave `Callable`/`Awaitable` imported now since Task 3 uses them immediately after.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_db_views.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add data/db/views.py tests/test_db_views.py
git commit -m "feat(db): market views repository — save and point-in-time read"
```

---

## Task 3: `data/db/views.py` — `structure_views`

**Files:**
- Modify: `data/db/views.py` (append)
- Test: `tests/test_db_views.py` (append)

**Interfaces:**
- Consumes: `save_market_view` from Task 2.
- Produces:
  - `Extractor = Callable[[uuid.UUID, str, str], Awaitable[dict[str, object]]]`
  - `structure_views(conn, *, extractor: Extractor | None = None, schema_version: int = 1, extraction_model: str = "claude-sonnet-4-5") -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db_views.py`:

```python
from data.db.views import structure_views  # add to the existing import line


async def test_structure_views_with_fake_extractor(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Bullish semis.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )

    async def fake_extractor(view_id: uuid.UUID, view_text: str, model: str) -> dict[str, object]:
        return {"stance": "bullish", "thesis": view_text}

    structured = await structure_views(pg_conn, extractor=fake_extractor)
    assert structured == 1

    row = await (
        await pg_conn.execute(
            "SELECT structure, extracted_by FROM market_view_structures WHERE view_id = %s",
            (view_id,),
        )
    ).fetchone()
    assert row[0]["stance"] == "bullish"
    assert row[1] == "claude-sonnet-4-5"


async def test_structure_views_is_idempotent_per_schema_version(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="Neutral.",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )

    async def fake_extractor(view_id: uuid.UUID, view_text: str, model: str) -> dict[str, object]:
        return {"stance": "neutral"}

    assert await structure_views(pg_conn, extractor=fake_extractor, schema_version=1) == 1
    assert await structure_views(pg_conn, extractor=fake_extractor, schema_version=1) == 0
    # A different schema_version is not blocked by the v1 row (todo.md A1.9).
    assert await structure_views(pg_conn, extractor=fake_extractor, schema_version=2) == 1


async def test_default_extractor_raises_not_implemented(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of=dt.date(2026, 8, 4),
        scope="market",
        view_text="x",
        model="hermes",
        data_cutoff=dt.date(2026, 8, 4),
    )
    with pytest.raises(NotImplementedError):
        await structure_views(pg_conn)
```

Update the import line at the top of `tests/test_db_views.py` to:
`from data.db.views import get_market_views, save_market_view, structure_views`

- [ ] **Step 2: Run tests to verify they fail**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_db_views.py -v`
Expected: FAIL — `ImportError: cannot import name 'structure_views'`.

- [ ] **Step 3: Append `structure_views` to `data/db/views.py`**

```python
Extractor = Callable[[uuid.UUID, str, str], Awaitable[dict[str, object]]]


async def _default_extractor(view_id: uuid.UUID, view_text: str, model: str) -> dict[str, object]:
    """Placeholder extractor — LLM wiring is unfinished roadmap work."""
    raise NotImplementedError(
        f"LLM extractor not wired yet (view {view_id}); see agent/prompts/view_structuring.md"
    )


async def structure_views(
    conn: psycopg.AsyncConnection,
    *,
    extractor: Extractor | None = None,
    schema_version: int = 1,
    extraction_model: str = "claude-sonnet-4-5",
) -> int:
    """Structure free-text views with no row for this schema_version yet.

    Idempotent per (view_id, schema_version): a view already structured
    under a given schema_version is skipped for that version, but can still
    be (re-)structured under a different schema_version — the DuckDB
    version's bug (todo.md A1.9) of blocking all future re-structuring once
    any row existed is fixed by the (view_id, schema_version) primary key.

    Args:
        conn: An open connection.
        extractor: Async callable(view_id, view_text, model) ->
            JSON-serialisable dict. Defaults to a stub that raises
            NotImplementedError.
        schema_version: Schema version stored with each extraction.
        extraction_model: Model identifier recorded for provenance.

    Returns:
        Number of views structured in this run.

    Raises:
        NotImplementedError: If no extractor is supplied and the default is used.
    """
    extract = extractor or _default_extractor

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT mv.view_id, mv.view_text, mv.written_by AS model
            FROM market_views mv
            LEFT JOIN market_view_structures mvs
              ON mvs.view_id = mv.view_id AND mvs.schema_version = %s
            WHERE mvs.view_id IS NULL
            ORDER BY mv.created_at
            """,
            (schema_version,),
        )
        pending = await cur.fetchall()

    structured = 0
    for row in pending:
        payload = await extract(row["view_id"], row["view_text"], row["model"])
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO market_view_structures "
                "(view_id, schema_version, structure, extracted_by) "
                "VALUES (%s, %s, %s, %s)",
                (row["view_id"], schema_version, Jsonb(payload), extraction_model),
            )
        structured += 1

    return structured
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_db_views.py -v`
Expected: all pass (12 tests total in this file).

- [ ] **Step 5: Commit**

```bash
git add data/db/views.py tests/test_db_views.py
git commit -m "feat(db): market views structuring job, versioned by schema_version"
```

---

## Task 4: Rewrite `agent/views.py`

**Files:**
- Modify: `agent/views.py` (full rewrite)
- Modify: `tests/test_views.py` (full rewrite)

**Interfaces:**
- Consumes: `data.db.views.save_market_view`, `data.db.views.get_market_views` (Task 2), `data.db.identifiers.resolve_identifier` (existing).
- Produces:
  - `save_market_view(conn, *, as_of: str, view_text: str, model: str, data_cutoff: str, scope: str = "market", ticker: str | None = None, theme: str | None = None, used_fact_ids: list[str] | None = None) -> str`
  - `get_market_views(conn, *, as_of: str, ticker: str | None = None, scope: str | None = None, limit: int = 50) -> list[dict[str, object]]`
  - `VALID_SCOPES = ("market", "ticker", "theme")` (unchanged)

`structure_views` is **not** re-exported here — callers use `data.db.views.structure_views` directly (Task 3's docstring already documents it; this module only wraps the two ticker-resolving functions).

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_views.py` entirely:

```python
"""Tests for the market-views orchestration layer — ticker resolution + validation."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from agent.views import get_market_views, save_market_view
from data.db.identifiers import get_or_create_identifier

pytestmark = pytest.mark.asyncio


async def _ticker(pg_conn, ticker: str = "AAPL") -> uuid.UUID:
    return await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value=ticker,
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name=f"{ticker} Inc",
        country="US",
        sector="Technology",
    )


async def test_save_and_get_round_trip(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="market",
    )
    views = await get_market_views(pg_conn, as_of="2026-08-05")
    assert len(views) == 1
    assert str(views[0]["view_id"]) == view_id
    assert views[0]["view_text"] == "Rates path is the key swing factor for H2."


async def test_save_and_get_used_fact_ids_round_trip(pg_conn) -> None:
    fact_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    view_id = await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="market",
        used_fact_ids=fact_ids,
    )
    views = await get_market_views(pg_conn, as_of="2026-08-05")
    assert str(views[0]["view_id"]) == view_id
    assert sorted(str(f) for f in views[0]["used_fact_ids"]) == sorted(fact_ids)


async def test_save_without_used_fact_ids_defaults_none(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="No provenance.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="market",
    )
    views = await get_market_views(pg_conn, as_of="2026-08-05")
    assert views[0]["used_fact_ids"] is None


async def test_get_market_views_filters_by_as_of(pg_conn) -> None:
    await save_market_view(
        pg_conn,
        as_of="2026-07-01",
        view_text="old",
        model="hermes",
        data_cutoff="2026-07-01",
    )
    await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="new",
        model="hermes",
        data_cutoff="2026-08-04",
    )
    assert len(await get_market_views(pg_conn, as_of="2026-07-15")) == 1
    views = await get_market_views(pg_conn, as_of="2026-08-05")
    assert [v["view_text"] for v in views] == ["new", "old"]


async def test_ticker_scope_validation(pg_conn) -> None:
    with pytest.raises(ValueError, match="ticker is required"):
        await save_market_view(
            pg_conn,
            as_of="2026-08-04",
            view_text="x",
            model="hermes",
            data_cutoff="2026-08-04",
            scope="ticker",
        )
    with pytest.raises(ValueError, match="only allowed"):
        await save_market_view(
            pg_conn,
            as_of="2026-08-04",
            view_text="x",
            model="hermes",
            data_cutoff="2026-08-04",
            ticker="AAPL",
        )


async def test_ticker_scope_resolves_to_company_id(pg_conn) -> None:
    company_id = await _ticker(pg_conn, "AAPL")
    view_id = await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="AAPL-specific view.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="ticker",
        ticker="AAPL",
    )
    views = await get_market_views(pg_conn, as_of="2026-08-04", ticker="AAPL")
    assert [str(v["view_id"]) for v in views] == [view_id]
    assert views[0]["company_id"] == company_id


async def test_ticker_scope_unresolvable_ticker_raises_on_write(pg_conn) -> None:
    with pytest.raises(ValueError, match="no company found for ticker 'NOPE'"):
        await save_market_view(
            pg_conn,
            as_of="2026-08-04",
            view_text="x",
            model="hermes",
            data_cutoff="2026-08-04",
            scope="ticker",
            ticker="NOPE",
        )


async def test_get_market_views_unresolvable_ticker_returns_empty(pg_conn) -> None:
    assert await get_market_views(pg_conn, as_of="2026-08-04", ticker="NOPE") == []


async def test_theme_scope_round_trip(pg_conn) -> None:
    view_id = await save_market_view(
        pg_conn,
        as_of="2026-08-04",
        view_text="Semis cycle turning up.",
        model="hermes",
        data_cutoff="2026-08-04",
        scope="theme",
        theme="semiconductors",
    )
    views = await get_market_views(pg_conn, as_of="2026-08-04", scope="theme")
    assert str(views[0]["view_id"]) == view_id


async def test_data_cutoff_is_required(pg_conn) -> None:
    with pytest.raises(TypeError):
        await save_market_view(  # type: ignore[call-arg]
            pg_conn,
            as_of="2026-08-04",
            view_text="x",
            model="hermes",
            scope="market",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_views.py -v`
Expected: FAIL — old `agent/views.py` still imports `duckdb` and its functions are sync with a different signature; collection or assertion errors across the board.

- [ ] **Step 3: Rewrite `agent/views.py`**

```python
"""Market view write + read — thin orchestration over data/db/views.py.

Resolves human-facing ticker strings to company_id before calling the
repository layer, and keeps the cross-field scope/ticker/theme validation
that lived here in the DuckDB version. All SQL lives in data/db/views.py;
this module contains none. `structure_views` is not wrapped here — callers
use `data.db.views.structure_views` directly, since it takes no ticker and
needs no resolution (see docs/superpowers/specs/2026-08-11-postgres-views-cutover-design.md).
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg

from data.db import views as views_repo
from data.db.identifiers import resolve_identifier

VALID_SCOPES = ("market", "ticker", "theme")


async def save_market_view(
    conn: psycopg.AsyncConnection,
    *,
    as_of: str,
    view_text: str,
    model: str,
    data_cutoff: str,
    scope: str = "market",
    ticker: str | None = None,
    theme: str | None = None,
    used_fact_ids: list[str] | None = None,
) -> str:
    """Insert a free-text market view (immutable), resolving ticker -> company_id.

    Args:
        conn: An open connection.
        as_of: Market date (YYYY-MM-DD) the view refers to.
        view_text: Free-text view content, never edited in place.
        model: Model identifier that produced the view.
        data_cutoff: Date (YYYY-MM-DD) of the newest data used to write the view.
        scope: 'market' | 'ticker' | 'theme'.
        ticker: Ticker symbol when scope == 'ticker'; resolved to a
            company_id as of `as_of`.
        theme: Theme label when scope == 'theme'.
        used_fact_ids: company_facts ids that informed this view (provenance).

    Returns:
        New view ID, as a string.

    Raises:
        ValueError: On invalid scope, missing/extra scope argument, bad
            date, or a ticker that does not resolve to a company as of
            `as_of`.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {VALID_SCOPES}, got {scope!r}")
    if scope == "ticker" and not ticker:
        raise ValueError("ticker is required when scope == 'ticker'")
    if scope == "theme" and not theme:
        raise ValueError("theme is required when scope == 'theme'")
    if scope != "ticker" and ticker:
        raise ValueError("ticker is only allowed when scope == 'ticker'")
    if scope != "theme" and theme:
        raise ValueError("theme is only allowed when scope == 'theme'")

    as_of_date = dt.date.fromisoformat(as_of)
    data_cutoff_date = dt.date.fromisoformat(data_cutoff)

    company_id: uuid.UUID | None = None
    if scope == "ticker":
        company_id = await resolve_identifier(
            conn, id_type="ticker", id_value=ticker, as_of=as_of_date
        )
        if company_id is None:
            raise ValueError(f"no company found for ticker {ticker!r} as of {as_of}")

    used_fact_uuids = [uuid.UUID(fid) for fid in used_fact_ids] if used_fact_ids else None

    view_id = await views_repo.save_market_view(
        conn,
        as_of=as_of_date,
        scope=scope,
        company_id=company_id,
        theme=theme,
        view_text=view_text,
        model=model,
        data_cutoff=data_cutoff_date,
        used_fact_ids=used_fact_uuids,
    )
    return str(view_id)


async def get_market_views(
    conn: psycopg.AsyncConnection,
    *,
    as_of: str,
    ticker: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return market views as of a date (as_of <= :as_of), newest first.

    Args:
        conn: An open connection.
        as_of: Date string (YYYY-MM-DD). Views referring to this date or earlier.
        ticker: Optional ticker filter. Resolved to a company_id as of
            `as_of`; a ticker that does not resolve returns an empty list
            rather than raising — a read is a filter, not an assertion that
            the ticker must exist.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        limit: Maximum number of views to return.

    Returns:
        List of view dicts ordered by as_of descending.

    Raises:
        ValueError: If as_of is not a valid date.
    """
    as_of_date = dt.date.fromisoformat(as_of)

    company_id: uuid.UUID | None = None
    if ticker:
        company_id = await resolve_identifier(
            conn, id_type="ticker", id_value=ticker, as_of=as_of_date
        )
        if company_id is None:
            return []

    return await views_repo.get_market_views(
        conn, as_of=as_of_date, company_id=company_id, scope=scope, limit=limit
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_views.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent/views.py tests/test_views.py
git commit -m "feat: port agent/views.py to postgres, resolving ticker to company_id"
```

---

## Task 5: Rewire `server/mcp.py`

**Files:**
- Modify: `server/mcp.py:1-20` (imports), `:735-756` (`get_market_views` tool), `:799-837` (`save_market_view_tool`), add `_connect_pg()` helper near `_connect()` (`:70-80`)
- Modify: `tests/test_mcp_tools.py:197-206`, `:249-262`

**Interfaces:**
- Consumes: `agent.views.save_market_view`, `agent.views.get_market_views` (Task 4).
- Produces: `_connect_pg()` async context manager in `server/mcp.py`; `get_market_views` and `save_market_view_tool` become `async def`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_mcp_tools.py`, add near the top imports:

```python
import uuid
from contextlib import asynccontextmanager
```

Replace the existing `test_market_views_tools` (currently lines 197-206):

```python
async def test_market_views_tools(monkeypatch, pg_conn) -> None:
    @asynccontextmanager
    async def _fake_connect_pg():
        yield pg_conn

    monkeypatch.setattr(mcp_mod, "_connect_pg", _fake_connect_pg)

    view_id = await mcp_mod.save_market_view_tool(
        as_of="2026-08-04",
        view_text="Equities skew to the upside.",
        model="hermes",
        data_cutoff="2026-08-04",
    )
    views = await mcp_mod.get_market_views(as_of="2026-08-04")
    assert str(views[0]["view_id"]) == view_id
```

Replace the existing `test_save_market_view_tool_stores_used_fact_ids` (currently lines 249-262):

```python
async def test_save_market_view_tool_stores_used_fact_ids(monkeypatch, pg_conn) -> None:
    @asynccontextmanager
    async def _fake_connect_pg():
        yield pg_conn

    monkeypatch.setattr(mcp_mod, "_connect_pg", _fake_connect_pg)

    fact_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    view_id = await mcp_mod.save_market_view_tool(
        as_of="2026-08-04",
        view_text="Rates path is the key swing factor for H2.",
        model="hermes",
        data_cutoff="2026-08-04",
        used_fact_ids=fact_ids,
    )
    views = await mcp_mod.get_market_views(as_of="2026-08-05")

    assert str(views[0]["view_id"]) == view_id
    assert sorted(str(f) for f in views[0]["used_fact_ids"]) == sorted(fact_ids)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_mcp_tools.py -k market_views -v`
Expected: FAIL — `mcp_mod` has no attribute `_connect_pg`; `save_market_view_tool` is still sync and takes no `data_cutoff`.

- [ ] **Step 3: Add imports and the `_connect_pg()` helper to `server/mcp.py`**

At the top of `server/mcp.py`, add to the existing import block (after `from collections.abc import Callable`):

```python
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
```

(replaces the existing `from collections.abc import Callable` line — combine into one)

Add, right after the existing imports, alongside the other top-level imports:

```python
import psycopg

from data.db.pool import dsn_from_env
```

Add a new helper immediately after `_connect()` (currently `server/mcp.py:70-80`):

```python
@asynccontextmanager
async def _connect_pg() -> AsyncIterator[psycopg.AsyncConnection]:
    """Open a PostgreSQL connection for the views tools, closing it after use.

    One connection per tool call — no pool lifecycle management here; that
    is separate, later work once more tools are wired onto Postgres.

    Yields:
        A connection scoped to this call.
    """
    conn = await psycopg.AsyncConnection.connect(dsn_from_env())
    try:
        yield conn
    finally:
        await conn.close()
```

- [ ] **Step 4: Rewire `get_market_views`**

Replace (currently `server/mcp.py:735-756`):

```python
@mcp.tool()
def get_market_views(
    as_of: str,
    ticker: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return historical market views as of a date.

    Args:
        as_of: Date (YYYY-MM-DD); returns views for this date or earlier.
        ticker: Optional ticker filter.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        limit: Maximum views to return.

    Returns:
        List of view dicts (id, as_of, scope, ticker, theme, view_text, model,
        data_cutoff, used_fact_ids, created_at) ordered by as_of descending.
    """
    with _connect() as conn:
        return get_views(conn, as_of=as_of, ticker=ticker, scope=scope, limit=limit)
```

With:

```python
@mcp.tool()
async def get_market_views(
    as_of: str,
    ticker: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return historical market views as of a date.

    Args:
        as_of: Date (YYYY-MM-DD); returns views for this date or earlier.
        ticker: Optional ticker filter; a ticker with no matching company
            as of `as_of` yields an empty result rather than an error.
        scope: Optional scope filter ('market' | 'ticker' | 'theme').
        limit: Maximum views to return.

    Returns:
        List of view dicts (view_id, as_of, scope, company_id, theme,
        view_text, model, data_cutoff, used_fact_ids, created_at) ordered by
        as_of descending.
    """
    async with _connect_pg() as conn:
        return await get_views(conn, as_of=as_of, ticker=ticker, scope=scope, limit=limit)
```

- [ ] **Step 5: Rewire `save_market_view_tool`**

Replace (currently `server/mcp.py:799-837`):

```python
@mcp.tool()
def save_market_view_tool(
    as_of: str,
    view_text: str,
    model: str,
    scope: str = "market",
    ticker: str | None = None,
    theme: str | None = None,
    data_cutoff: str | None = None,
    used_fact_ids: list[str] | None = None,
) -> str:
    """Persist a free-text market view (immutable once written).

    Args:
        as_of: Market date (YYYY-MM-DD) the view refers to.
        view_text: Free-text view content.
        model: Model identifier that produced the view.
        scope: 'market' | 'ticker' | 'theme'.
        ticker: Required when scope == 'ticker'.
        theme: Required when scope == 'theme'.
        data_cutoff: ISO timestamp of newest data used, optional.
        used_fact_ids: ticker_facts ids that informed this view, optional.

    Returns:
        New view ID.
    """
    with _connect() as conn:
        return save_view(
            conn,
            as_of=as_of,
            view_text=view_text,
            model=model,
            scope=scope,
            ticker=ticker,
            theme=theme,
            data_cutoff=data_cutoff,
            used_fact_ids=used_fact_ids,
        )
```

With:

```python
@mcp.tool()
async def save_market_view_tool(
    as_of: str,
    view_text: str,
    model: str,
    data_cutoff: str,
    scope: str = "market",
    ticker: str | None = None,
    theme: str | None = None,
    used_fact_ids: list[str] | None = None,
) -> str:
    """Persist a free-text market view (immutable once written).

    Args:
        as_of: Market date (YYYY-MM-DD) the view refers to.
        view_text: Free-text view content.
        model: Model identifier that produced the view.
        data_cutoff: Date (YYYY-MM-DD) of the newest data used to write the
            view. Required.
        scope: 'market' | 'ticker' | 'theme'.
        ticker: Required when scope == 'ticker'; must resolve to a known
            company as of `as_of`.
        theme: Required when scope == 'theme'.
        used_fact_ids: company_facts ids that informed this view, optional.

    Returns:
        New view ID.
    """
    async with _connect_pg() as conn:
        return await save_view(
            conn,
            as_of=as_of,
            view_text=view_text,
            model=model,
            data_cutoff=data_cutoff,
            scope=scope,
            ticker=ticker,
            theme=theme,
            used_fact_ids=used_fact_ids,
        )
```

- [ ] **Step 6: Run the targeted tests to verify they pass**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/test_mcp_tools.py -v`
Expected: all pass, including the untouched DuckDB-backed tests in the same file.

- [ ] **Step 7: Commit**

```bash
git add server/mcp.py tests/test_mcp_tools.py
git commit -m "feat(mcp): rewire market-views tools onto postgres"
```

---

## Task 6: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite with coverage**

Run: `MARKETMIND_REQUIRE_POSTGRES=1 uv run pytest tests/ -q --cov=. --cov-report=term-missing --cov-fail-under=85`
Expected: all tests pass, coverage ≥ 85%. Check the report specifically for `data/db/views.py` and `agent/views.py` coverage — both should be at or near 100% given the tests written in Tasks 2–4.

- [ ] **Step 2: Run lint and format checks**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: both clean, no diffs.

- [ ] **Step 3: If ruff format finds issues, fix and re-verify**

Run: `uv run ruff format .` then re-run Steps 1–2.

- [ ] **Step 4: Grep the diff for stray DuckDB/duckdb references in touched files**

Run: `git diff main --stat && git diff main -- agent/views.py server/mcp.py data/db/views.py | grep -i duckdb`
Expected: no output (no `duckdb` import or reference left in the three rewritten files).

- [ ] **Step 5: Confirm no AI/tool attribution anywhere in the diff**

Run: `git log main..HEAD --format='%B' | grep -iE "generated with|co-authored-by|claude|anthropic|gpt|openai"`
Expected: no output.
