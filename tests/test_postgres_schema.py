"""Verifies the applied PostgreSQL schema and the pgvector layer."""

from __future__ import annotations

import pytest
from pgvector import Vector

from data.db.init_postgres import apply_schema
from data.db.pool import dsn_from_env

pytestmark = pytest.mark.asyncio


async def test_schema_applies_and_creates_the_expected_objects(pg_conn) -> None:
    tables = await (
        await pg_conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        )
    ).fetchone()
    assert tables[0] == 32


async def test_schema_application_records_baseline_and_is_idempotent(pg_conn) -> None:
    await apply_schema(dsn_from_env())

    rows = await (
        await pg_conn.execute(
            "SELECT version, name, checksum FROM marketmind_meta.schema_migrations ORDER BY version"
        )
    ).fetchall()
    assert [(version, name) for version, name, _checksum in rows] == [
        (1, "production_baseline"),
        (2, "complete_postgres_cutover"),
        (3, "rss_ingest_efficiency"),
        (4, "sec_company_financials"),
    ]


async def test_vector_column_and_hnsw_index_exist(pg_conn) -> None:
    row = await (
        await pg_conn.execute(
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
        await pg_conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='fact_embeddings' AND indexname='idx_fact_embeddings_hnsw'"
        )
    ).fetchone()
    assert index is not None, "HNSW index missing"
    assert "hnsw" in index[0] and "vector_cosine_ops" in index[0]


async def test_financials_schema_has_bitemporal_access_paths(pg_conn) -> None:
    columns = await (
        await pg_conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='financials'"
        )
    ).fetchall()
    column_types = dict(columns)
    assert column_types["company_id"] == "uuid"
    assert column_types["filed_at"] == "date"
    assert column_types["accession_number"] == "text"
    assert column_types["value"] == "numeric"

    indexes = await (
        await pg_conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='financials'"
        )
    ).fetchall()
    definitions = dict(indexes)
    assert "NULLS NOT DISTINCT" in definitions["uq_financials_observation"]
    assert "filed_at DESC" in definitions["idx_financials_pit"]


async def test_cosine_similarity_orders_by_closeness(pg_conn) -> None:
    """The nearest vector must rank first and similarity must fall in [0, 1].

    Embeddings are bound as plain Python lists. `register_vector_async`
    (registered per-connection by `open_pool`) only registers dumpers for
    `pgvector.Vector` and `numpy.ndarray`, but a plain `list` still works as
    an INSERT parameter for a `vector` column via pgvector's assignment
    cast from `double precision[]`. In an expression context such as
    `embedding <=> %s`, there is no target column to drive that cast, so an
    explicit `::vector` cast is required.
    """
    async with pg_conn.transaction():
        await pg_conn.execute(
            "INSERT INTO companies (company_id, name, first_seen) "
            "VALUES ('11111111-1111-1111-1111-111111111111', 'TestCo', '2020-01-01')"
        )
        await pg_conn.execute(
            "INSERT INTO articles (article_id, url_hash, content_hash, url, source, published_at) "
            "VALUES ('22222222-2222-2222-2222-222222222222','h','c','http://x','test',NOW())"
        )
        for fact_id, vec in (
            ("33333333-3333-3333-3333-333333333331", [1.0] + [0.0] * 1535),
            ("33333333-3333-3333-3333-333333333332", [0.0, 1.0] + [0.0] * 1534),
        ):
            await pg_conn.execute(
                "INSERT INTO company_facts "
                "(fact_id, company_id, fact_type, statement, confidence, valid_from, "
                "source_article) "
                "VALUES (%s,'11111111-1111-1111-1111-111111111111','risk','s',0.5,'2020-01-01',"
                "'22222222-2222-2222-2222-222222222222')",
                (fact_id,),
            )
            await pg_conn.execute(
                "INSERT INTO fact_embeddings (fact_id, model, embedding) VALUES (%s, 'test', %s)",
                (fact_id, vec),
            )

    query_vec = [1.0] + [0.0] * 1535
    rows = await (
        await pg_conn.execute(
            "SELECT fact_id, 1 - (embedding <=> %s::vector) AS similarity "
            "FROM fact_embeddings ORDER BY embedding <=> %s::vector",
            (query_vec, query_vec),
        )
    ).fetchall()

    assert str(rows[0][0]) == "33333333-3333-3333-3333-333333333331"
    assert rows[0][1] == pytest.approx(1.0, abs=1e-6)
    assert 0.0 <= rows[1][1] <= 1.0
    assert rows[0][1] > rows[1][1]


async def test_pool_vector_type_registration_survives_the_schema_reset(pg_conn) -> None:
    """A `pgvector.Vector` bound through the fixture's pool must round-trip.

    `open_pool()` registers the pgvector type once per physical connection,
    at connect time, baking in the `vector` type's current OID. Because
    `postgres_pool` (session-scoped) resets and re-applies the schema
    before opening the pool this connection comes from, every connection
    the test suite sees is already registered against the extension's
    final OID — this is a regression test for that ordering.
    """
    async with pg_conn.transaction():
        await pg_conn.execute(
            "INSERT INTO companies (company_id, name, first_seen) "
            "VALUES ('44444444-4444-4444-4444-444444444444', 'VectorRoundTripCo', "
            "'2020-01-01')"
        )
        await pg_conn.execute(
            "INSERT INTO articles (article_id, url_hash, content_hash, url, source, "
            "published_at) VALUES ('55555555-5555-5555-5555-555555555555','h2','c2',"
            "'http://y','test',NOW())"
        )
        await pg_conn.execute(
            "INSERT INTO company_facts "
            "(fact_id, company_id, fact_type, statement, confidence, valid_from, "
            "source_article) VALUES ('66666666-6666-6666-6666-666666666666',"
            "'44444444-4444-4444-4444-444444444444','risk','s',0.5,'2020-01-01',"
            "'55555555-5555-5555-5555-555555555555')"
        )
        vec = Vector([1.0] + [0.0] * 1535)
        await pg_conn.execute(
            "INSERT INTO fact_embeddings (fact_id, model, embedding) VALUES (%s, 'test', %s)",
            ("66666666-6666-6666-6666-666666666666", vec),
        )
    row = await (
        await pg_conn.execute(
            "SELECT embedding FROM fact_embeddings WHERE fact_id = %s",
            ("66666666-6666-6666-6666-666666666666",),
        )
    ).fetchone()

    assert isinstance(row[0], Vector)
    assert row[0].to_list() == [1.0] + [0.0] * 1535
