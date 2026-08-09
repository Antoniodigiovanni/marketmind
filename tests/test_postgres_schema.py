"""Applies the PostgreSQL schema and validates the pgvector layer."""

from __future__ import annotations

import pytest

from data.db.init_postgres import apply_schema
from data.db.postgres import close_pool, database_available, open_pool

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def pool():
    if not await database_available():
        pytest.skip("no PostgreSQL reachable; start it with `docker compose up -d`")
    pool = await open_pool()
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await apply_schema(pool)
    yield pool
    await close_pool(pool)


async def test_schema_applies_and_creates_the_expected_objects(pool) -> None:
    async with pool.connection() as conn:
        tables = await (
            await conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            )
        ).fetchone()
    assert tables[0] == 30


async def test_schema_is_idempotent(pool) -> None:
    """schema.sql is re-applied on every init; a second run must not error."""
    await apply_schema(pool)


async def test_vector_column_and_hnsw_index_exist(pool) -> None:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                SELECT a.atttypmod
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = 'fact_embeddings' AND a.attname = 'embedding'
                """
            )
        ).fetchone()
        assert row[0] == 1536, "embedding column is not vector(1536)"

        index = await (
            await conn.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename='fact_embeddings' AND indexname='idx_fact_embeddings_hnsw'"
            )
        ).fetchone()
    assert index is not None, "HNSW index missing"
    assert "hnsw" in index[0] and "vector_cosine_ops" in index[0]


async def test_cosine_similarity_orders_by_closeness(pool) -> None:
    """The nearest vector must rank first and similarity must fall in [0, 1].

    Embeddings are bound as plain Python lists. `register_vector_async`
    (registered per-connection by `open_pool`) only registers dumpers for
    `pgvector.Vector` and `numpy.ndarray`, but a plain `list` still works as
    an INSERT parameter for a `vector` column: pgvector ships an assignment
    cast from `double precision[]` to `vector`, which Postgres applies
    automatically once the target column type is known (verified empirically
    against this DB -- a mismatched-length list correctly raises a dimension
    error rather than silently inserting). In an expression context such as
    `embedding <=> %s`, there is no target column to drive that cast, so the
    parameter resolves to `double precision[]` and an explicit `::vector`
    cast is required -- confirmed empirically too (omitting it raises
    `UndefinedFunction: operator does not exist: vector <=> double precision[]`).
    """
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO companies (company_id, name, first_seen) "
            "VALUES ('11111111-1111-1111-1111-111111111111', 'TestCo', '2020-01-01')"
        )
        await conn.execute(
            "INSERT INTO articles (article_id, url_hash, content_hash, url, source, published_at) "
            "VALUES ('22222222-2222-2222-2222-222222222222','h','c','http://x','test',NOW())"
        )
        for fact_id, vec in (
            ("33333333-3333-3333-3333-333333333331", [1.0] + [0.0] * 1535),
            ("33333333-3333-3333-3333-333333333332", [0.0, 1.0] + [0.0] * 1534),
        ):
            await conn.execute(
                "INSERT INTO company_facts "
                "(fact_id, company_id, fact_type, statement, confidence, valid_from, source_article) "
                "VALUES (%s,'11111111-1111-1111-1111-111111111111','risk','s',0.5,'2020-01-01',"
                "'22222222-2222-2222-2222-222222222222')",
                (fact_id,),
            )
            await conn.execute(
                "INSERT INTO fact_embeddings (fact_id, model, embedding) VALUES (%s, 'test', %s)",
                (fact_id, vec),
            )

        query_vec = [1.0] + [0.0] * 1535
        rows = await (
            await conn.execute(
                "SELECT fact_id, 1 - (embedding <=> %s::vector) AS similarity "
                "FROM fact_embeddings ORDER BY embedding <=> %s::vector",
                (query_vec, query_vec),
            )
        ).fetchall()

    assert str(rows[0][0]) == "33333333-3333-3333-3333-333333333331"
    assert rows[0][1] == pytest.approx(1.0, abs=1e-6)
    assert 0.0 <= rows[1][1] <= 1.0
    assert rows[0][1] > rows[1][1]
