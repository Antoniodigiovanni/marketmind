"""Regression coverage for atomic, dimension-scoped aggregate fact supersession."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from data.db.aggregate_facts import create_aggregate_fact, get_current_aggregate_facts
from data.db.companies import create_company
from data.db.pool import dsn_from_env

pytestmark = pytest.mark.asyncio
START = dt.date(2020, 1, 1)
REPLACEMENT = dt.date(2021, 1, 1)
VECTOR = [1.0] + [0.0] * 1535


async def _embed(text: str) -> list[float]:
    return VECTOR


async def _subjects(conn: psycopg.AsyncConnection) -> tuple[uuid.UUID, uuid.UUID]:
    company_id = await create_company(
        conn, name="Supersession test", country="US", sector="Technology", first_seen=START
    )
    article = await (
        await conn.execute(
            "INSERT INTO articles (url_hash, content_hash, url, source, published_at) "
            "VALUES (%s, %s, %s, 'test', NOW()) RETURNING article_id",
            (uuid.uuid4().hex, uuid.uuid4().hex, "https://example.invalid/supersession"),
        )
    ).fetchone()
    return company_id, article[0]


async def _fact(
    conn: psycopg.AsyncConnection,
    company_id: uuid.UUID,
    article_id: uuid.UUID,
    *,
    statement: str = "Predecessor",
    valid_from: dt.date = START,
    supersedes_id: uuid.UUID | None = None,
    embed: Callable[[str], Awaitable[list[float]]] = _embed,
    dimension: str = "sector",
    evidence: list[uuid.UUID] | None = None,
) -> uuid.UUID:
    return await create_aggregate_fact(
        conn,
        dimension=dimension,
        dimension_key=str(company_id),
        statement=statement,
        fact_type="trend",
        confidence=0.9,
        valid_from=valid_from,
        supersedes_id=supersedes_id,
        evidence=evidence or [],
        embed=embed,
        model="test",
    )


async def _snapshot(conn: psycopg.AsyncConnection, company_id: uuid.UUID) -> list[tuple]:
    return await (
        await conn.execute(
            "SELECT f.aggregate_id, f.statement, f.valid_from, f.valid_until, "
            "f.dimension, e.model, e.embedding::text, "
            "ARRAY(SELECT fact_id FROM aggregate_fact_evidence a "
            "WHERE a.aggregate_id = f.aggregate_id ORDER BY fact_id) "
            "FROM aggregate_facts f "
            "LEFT JOIN aggregate_fact_embeddings e USING (aggregate_id) "
            "WHERE f.dimension_key = %s ORDER BY f.aggregate_id",
            (str(company_id),),
        )
    ).fetchall()


async def test_supersession_preserves_exact_point_in_time_boundary(pg_conn) -> None:
    company_id, article_id = await _subjects(pg_conn)
    old_id = await _fact(pg_conn, company_id, article_id)
    new_id = await _fact(
        pg_conn,
        company_id,
        article_id,
        statement="Replacement",
        valid_from=REPLACEMENT,
        supersedes_id=old_id,
    )
    before = await get_current_aggregate_facts(
        pg_conn,
        dimension="sector",
        dimension_key=str(company_id),
        as_of=REPLACEMENT - dt.timedelta(days=1),
    )
    at = await get_current_aggregate_facts(
        pg_conn, dimension="sector", dimension_key=str(company_id), as_of=REPLACEMENT
    )
    assert [row["aggregate_id"] for row in before] == [old_id]
    assert [row["aggregate_id"] for row in at] == [new_id]
    rows = {row[0]: row for row in await _snapshot(pg_conn, company_id)}
    assert rows[old_id][3] == REPLACEMENT
    assert rows[new_id][3:5] == (None, "sector")
    assert all(row[5] == "test" and row[6] is not None for row in rows.values())


@pytest.mark.parametrize(
    "invalid", ["wrong_key", "wrong_dimension", "missing", "closed", "stale", "backward", "equal"]
)
async def test_invalid_supersession_is_value_error_and_changes_nothing(
    pg_conn, invalid: str
) -> None:
    company_id, article_id = await _subjects(pg_conn)
    other_id, _ = await _subjects(pg_conn)
    owner_id = other_id if invalid == "wrong_key" else company_id
    old_id = await _fact(
        pg_conn,
        owner_id,
        article_id,
        dimension="geography" if invalid == "wrong_dimension" else "sector",
    )
    if invalid == "closed":
        await pg_conn.execute(
            "UPDATE aggregate_facts SET valid_until = %s WHERE aggregate_id = %s",
            (REPLACEMENT, old_id),
        )
    if invalid == "stale":
        await _fact(
            pg_conn,
            company_id,
            article_id,
            statement="First replacement",
            valid_from=REPLACEMENT,
            supersedes_id=old_id,
        )
    predecessor = uuid.uuid4() if invalid == "missing" else old_id
    replacement_date = {
        "backward": START - dt.timedelta(days=1),
        "equal": START,
    }.get(invalid, REPLACEMENT + dt.timedelta(days=1))
    before = [await _snapshot(pg_conn, subject) for subject in (company_id, other_id)]
    with pytest.raises(ValueError):
        await _fact(
            pg_conn,
            company_id,
            article_id,
            statement="Rejected replacement",
            valid_from=replacement_date,
            supersedes_id=predecessor,
        )
    assert [await _snapshot(pg_conn, subject) for subject in (company_id, other_id)] == before


async def test_embedding_write_failure_preserves_predecessor_and_leaves_no_replacement(
    pg_conn,
) -> None:
    company_id, article_id = await _subjects(pg_conn)
    old_id = await _fact(pg_conn, company_id, article_id)
    before = await _snapshot(pg_conn, company_id)

    async def wrong_dimension(text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    with pytest.raises(psycopg.errors.DataException):
        await _fact(
            pg_conn,
            company_id,
            article_id,
            statement="Failed replacement",
            valid_from=REPLACEMENT,
            supersedes_id=old_id,
            embed=wrong_dimension,
        )
    assert await _snapshot(pg_conn, company_id) == before
    assert [
        row["aggregate_id"]
        for row in await get_current_aggregate_facts(
            pg_conn, dimension="sector", dimension_key=str(company_id), as_of=REPLACEMENT
        )
    ] == [old_id]


async def test_evidence_write_failure_rolls_back_replacement_and_embedding(pg_conn) -> None:
    company_id, article_id = await _subjects(pg_conn)
    old_id = await _fact(pg_conn, company_id, article_id)
    before = await _snapshot(pg_conn, company_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await _fact(
            pg_conn,
            company_id,
            article_id,
            statement="Failed evidence replacement",
            valid_from=REPLACEMENT,
            supersedes_id=old_id,
            evidence=[uuid.uuid4()],
        )
    assert await _snapshot(pg_conn, company_id) == before


async def test_competing_committed_replacements_have_one_winner(postgres_pool) -> None:
    """Observe actual lock contention, then commit the winner before checking the loser."""
    # postgres_pool applies the schema only after enforcing the disposable _test guard.
    # Independent physical connections use the same configured DSN as that fixture.
    async with AsyncExitStack() as stack:
        connections = [
            await stack.enter_async_context(
                await psycopg.AsyncConnection.connect(
                    dsn_from_env(), autocommit=True, connect_timeout=3
                )
            )
            for _ in range(3)
        ]
        winner, loser, observer = connections
        assert len({conn.info.backend_pid for conn in connections}) == 3
        company_id = article_id = None
        contender = None
        try:
            async with observer.transaction():
                company_id, article_id = await _subjects(observer)
                old_id = await _fact(observer, company_id, article_id)
            baseline = await _snapshot(observer, company_id)
            for conn in (winner, loser):
                await conn.execute("SET statement_timeout = '15s'")

            async def replace_loser() -> uuid.UUID:
                return await _fact(
                    loser,
                    company_id,
                    article_id,
                    statement="Losing replacement",
                    valid_from=REPLACEMENT + dt.timedelta(days=1),
                    supersedes_id=old_id,
                )

            async with winner.transaction():
                winner_id = await _fact(
                    winner,
                    company_id,
                    article_id,
                    statement="Winning replacement",
                    valid_from=REPLACEMENT,
                    supersedes_id=old_id,
                )
                contender = asyncio.create_task(replace_loser())
                async with asyncio.timeout(10):
                    while True:
                        blockers = await (
                            await observer.execute(
                                "SELECT pg_blocking_pids(%s)", (loser.info.backend_pid,)
                            )
                        ).fetchone()
                        if winner.info.backend_pid in blockers[0]:
                            break
                        if contender.done():
                            pytest.fail("competing writer finished without waiting for predecessor")
                        await asyncio.sleep(0.01)
                assert not contender.done()
                assert await _snapshot(observer, company_id) == baseline
            # The winner's outer transaction has committed. The losing repository
            # call must roll back its own transaction and return a domain error.
            with pytest.raises(ValueError):
                await asyncio.wait_for(contender, timeout=10)
            assert loser.info.transaction_status == TransactionStatus.IDLE
            assert await _snapshot(loser, company_id) == await _snapshot(observer, company_id)
            rows = {row[0]: row for row in await _snapshot(observer, company_id)}
            assert set(rows) == {old_id, winner_id}
            assert rows[old_id][3] == REPLACEMENT
            assert rows[winner_id][3:5] == (None, "sector")
            assert all(row[5] == "test" and row[6] is not None for row in rows.values())
            assert [
                row["aggregate_id"]
                for row in await get_current_aggregate_facts(
                    observer,
                    dimension="sector",
                    dimension_key=str(company_id),
                    as_of=REPLACEMENT + dt.timedelta(days=2),
                )
            ] == [winner_id]
        finally:
            if contender is not None:
                if not contender.done():
                    contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
            # Delete only this test's UUID-scoped rows, including any erroneous loser.
            async with observer.transaction():
                if company_id is not None:
                    await observer.execute(
                        "DELETE FROM aggregate_facts WHERE dimension_key = %s",
                        (str(company_id),),
                    )
                    await observer.execute(
                        "DELETE FROM companies WHERE company_id = %s", (company_id,)
                    )
                if article_id is not None:
                    await observer.execute(
                        "DELETE FROM articles WHERE article_id = %s", (article_id,)
                    )
