"""SEC filings pipeline PostgreSQL tests."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import psycopg
import pytest

from data.db.filings import get_checkpoint_date
from data.db.identifiers import get_or_create_identifier
from data.db.pool import dsn_from_env
from data.filings import fetch
from data.filings.fetch import run_daily_fetch
from data.sources.sec import Submission

COMPANYFACTS_FIXTURE = Path(__file__).parent / "fixtures" / "sec" / "companyfacts_aapl.json"


class FakeClient:
    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        return {"AAPL": {"cik": "0000320193", "name": "Apple Inc."}}

    def submissions(self, cik: str) -> list[Submission]:
        return [
            Submission(
                accession="000032019326000001",
                form_type="8-K",
                filed_at="2026-08-30",
                period_of_report="2026-08-30",
                primary_document="report.htm",
            ),
            Submission(
                accession="000032019326000002",
                form_type="10-Q",
                filed_at="2026-08-30",
                period_of_report="2026-06-30",
                primary_document="quarterly.htm",
            ),
            Submission(
                accession="000032019320000001",
                form_type="10-K",
                filed_at="2020-11-01",
                period_of_report="2020-09-30",
                primary_document="old.htm",
            ),
        ]

    def download(self, cik: str, accession: str, document: str) -> str:
        return "<html><body><h1>Results</h1><p>Revenue increased.</p></body></html>"

    def companyfacts(self, cik: str) -> dict[str, object]:
        return json.loads(COMPANYFACTS_FIXTURE.read_text(encoding="utf-8"))


class FlakyClient(FakeClient):
    """Fail the current filing until explicitly recovered."""

    failing = True

    def download(self, cik: str, accession: str, document: str) -> str:
        if self.failing:
            raise RuntimeError("temporary archive failure")
        return super().download(cik, accession, document)


async def test_daily_filings_fetch_is_idempotent(pg_conn, tmp_path) -> None:
    await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )
    universe = pl.DataFrame({"ticker": ["AAPL"]})
    first = await run_daily_fetch(
        pg_conn,
        FakeClient(),
        run_date="2026-08-31",
        root=tmp_path,
        universe=universe,
    )
    second = await run_daily_fetch(
        pg_conn,
        FakeClient(),
        run_date="2026-08-31",
        root=tmp_path,
        universe=universe,
    )
    assert first["filings_parsed"] == 2
    assert second["filings_parsed"] == 0
    assert first["financial_issuers"] == 1
    assert first["financial_facts"] == 3
    assert second["financial_facts"] == 0
    assert (await (await pg_conn.execute("SELECT COUNT(*) FROM financials")).fetchone())[0] == 3
    assert (tmp_path / "filings" / "0000320193" / "000032019326000001.html").exists()
    assert not (tmp_path / "filings" / "0000320193" / "000032019320000001.html").exists()
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )


async def test_failed_filing_does_not_advance_checkpoint(pg_conn, tmp_path) -> None:
    """A transient document failure remains eligible on the next run."""
    await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )
    client = FlakyClient()
    universe = pl.DataFrame({"ticker": ["AAPL"]})

    failed = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )
    assert failed["filings_skipped"] == 2
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") is None

    client.failing = False
    recovered = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )
    assert recovered["filings_parsed"] == 2
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )


class FactsOnlyFailureClient(FakeClient):
    """Serve filings normally but fail the company facts refresh."""

    def companyfacts(self, cik: str) -> dict[str, object]:
        raise RuntimeError("company facts unavailable")


async def test_company_facts_failure_does_not_hold_back_the_filings_checkpoint(
    pg_conn, tmp_path
) -> None:
    """Facts are a separate concern: only filing failures pin the checkpoint."""
    await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )
    universe = pl.DataFrame({"ticker": ["AAPL"]})

    counts = await run_daily_fetch(
        pg_conn,
        FactsOnlyFailureClient(),
        run_date="2026-08-31",
        root=tmp_path,
        universe=universe,
    )

    assert counts["filings_parsed"] == 2
    assert counts["financials_skipped"] == 1
    assert counts["financial_issuers"] == 0
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )


class NonPeriodicClient(FakeClient):
    """Serve only an 8-K, and record every Company Facts request."""

    def __init__(self) -> None:
        self.companyfacts_calls = 0

    def submissions(self, cik: str) -> list[Submission]:
        return [
            Submission(
                accession="000032019326000003",
                form_type="8-K",
                filed_at="2026-08-30",
                period_of_report="2026-08-30",
                primary_document="report.htm",
            )
        ]

    def companyfacts(self, cik: str) -> dict[str, object]:
        self.companyfacts_calls += 1
        return super().companyfacts(cik)


async def test_company_facts_refresh_is_skipped_without_a_periodic_report(
    pg_conn, tmp_path
) -> None:
    """Company Facts only changes on periodic reports, so nothing else pulls it."""
    await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )
    client = NonPeriodicClient()
    universe = pl.DataFrame({"ticker": ["AAPL"]})

    counts = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )

    assert counts["filings_parsed"] == 1
    assert client.companyfacts_calls == 0
    assert counts["financial_issuers"] == 0
    assert counts["financial_facts"] == 0
    assert (await (await pg_conn.execute("SELECT COUNT(*) FROM financials")).fetchone())[0] == 0
    assert await get_checkpoint_date(pg_conn, source="submissions", key="AAPL") == dt.date(
        2026, 8, 31
    )


async def test_company_facts_refresh_follows_a_periodic_report(pg_conn, tmp_path) -> None:
    """A 10-Q pulls facts once; a later run with nothing new pulls nothing."""
    await get_or_create_identifier(
        pg_conn,
        id_type="ticker",
        id_value="AAPL",
        valid_from=dt.date(2000, 1, 1),
        source="test",
        name="Apple",
        country="US",
        sector="Technology",
    )

    class CountingClient(FakeClient):
        def __init__(self) -> None:
            self.companyfacts_calls = 0

        def companyfacts(self, cik: str) -> dict[str, object]:
            self.companyfacts_calls += 1
            return super().companyfacts(cik)

    client = CountingClient()
    universe = pl.DataFrame({"ticker": ["AAPL"]})

    first = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )
    second = await run_daily_fetch(
        pg_conn, client, run_date="2026-08-31", root=tmp_path, universe=universe
    )

    assert first["financial_issuers"] == 1
    assert first["financial_facts"] == 3
    assert second["financial_issuers"] == 0
    assert client.companyfacts_calls == 1


def test_main_bounds_the_run_and_the_universe_by_the_same_date(monkeypatch) -> None:
    """A backdated run must resolve that date's membership, not today's."""
    import sys

    from data.filings import fetch as fetch_module

    captured: dict[str, object] = {}

    def fake_get_universe(as_of: str) -> pl.DataFrame:
        captured["universe_as_of"] = as_of
        return pl.DataFrame({"ticker": ["AAPL"]})

    async def fake_run(client, *, run_date, root, universe, progress=None) -> dict[str, int]:
        captured["run_date"] = run_date
        return {"filings_fetched": 0}

    monkeypatch.setattr(fetch_module, "get_universe", fake_get_universe)
    monkeypatch.setattr(fetch_module, "run_daily_fetch_with_pool", fake_run)
    monkeypatch.setattr(fetch_module, "EdgardClient", lambda: object())
    monkeypatch.setattr(sys, "argv", ["fetch", "--run-date", "2026-08-31"])

    fetch_module.main()

    assert captured["run_date"] == "2026-08-31"
    assert captured["universe_as_of"] == "2026-08-31"


def test_main_defaults_to_today_and_rejects_a_malformed_date(monkeypatch) -> None:
    import sys

    from data.filings import fetch as fetch_module

    captured: dict[str, object] = {}

    async def fake_run(client, *, run_date, root, universe, progress=None) -> dict[str, int]:
        captured["run_date"] = run_date
        return {}

    monkeypatch.setattr(fetch_module, "get_universe", lambda as_of: pl.DataFrame({"ticker": []}))
    monkeypatch.setattr(fetch_module, "run_daily_fetch_with_pool", fake_run)
    monkeypatch.setattr(fetch_module, "EdgardClient", lambda: object())

    monkeypatch.setattr(sys, "argv", ["fetch"])
    fetch_module.main()
    assert captured["run_date"] == dt.date.today().isoformat()

    monkeypatch.setattr(sys, "argv", ["fetch", "--run-date", "31-08-2026"])
    try:
        fetch_module.main()
    except ValueError:
        pass
    else:  # pragma: no cover - the assertion below reports the failure
        raise AssertionError("main() accepted a malformed --run-date")


async def test_child_failure_rolls_back_durable_accession(postgres_pool, tmp_path, monkeypatch):
    """A failed child write must leave no committed accession to skip on retry."""
    import psycopg

    from data.db.pool import dsn_from_env
    from data.filings import fetch

    async def fail_child(conn: psycopg.AsyncConnection, **kwargs) -> None:
        raise RuntimeError("child persistence failed")

    monkeypatch.setattr(fetch, "insert_filing_text", fail_child)
    sub = FakeClient().submissions("0000320193")[0]
    # Connect directly so the observer does not depend on the session pool's worker loop.
    async with (
        postgres_pool.connection() as writer,
        await psycopg.AsyncConnection.connect(dsn_from_env(), autocommit=True) as observer,
    ):
        import pytest

        with pytest.raises(RuntimeError, match="child persistence failed"):
            await fetch._process_document(
                writer,
                FakeClient(),
                tmp_path,
                cik="0000320193",
                company_id=None,
                ticker="AAPL",
                sub=sub,
            )
        assert not await fetch.filing_exists(observer, accession=sub.accession)


# These tests deliberately commit through the pool rather than pg_conn's rollback
# fixture: an independent connection must observe exactly what a later run sees.


@pytest.fixture
async def durable_filing(postgres_pool, tmp_path, monkeypatch):
    """Provide isolated committed data and an independent observer, then remove it."""
    token = uuid.uuid4().hex
    ticker = f"ATOMIC{token}"
    cik = str(int(token[:8], 16)).zfill(10)
    accession = token
    run_ids = []
    real_start_run = fetch.start_run

    async def record_run(conn: psycopg.AsyncConnection, **kwargs) -> uuid.UUID:
        run_id = await real_start_run(conn, **kwargs)
        run_ids.append(run_id)
        return run_id

    monkeypatch.setattr(fetch, "start_run", record_run)
    async with (
        postgres_pool.connection() as writer,
        await psycopg.AsyncConnection.connect(
            dsn_from_env(), autocommit=True, connect_timeout=3
        ) as observer,
    ):
        company_id = await get_or_create_identifier(
            writer,
            id_type="ticker",
            id_value=ticker,
            valid_from=dt.date(2000, 1, 1),
            source="test",
            name=ticker,
            country=None,
            sector=None,
        )
        try:
            yield SimpleNamespace(
                writer=writer,
                observer=observer,
                ticker=ticker,
                cik=cik,
                accession=accession,
                root=tmp_path,
                company_id=company_id,
            )
        finally:
            await writer.execute("DELETE FROM filings WHERE accession = %s", (accession,))
            await writer.execute(
                "DELETE FROM fetch_checkpoints WHERE source = 'submissions' AND key = %s",
                (ticker,),
            )
            await writer.execute("DELETE FROM agent_runs WHERE run_id = ANY(%s)", (run_ids,))
            await writer.execute("DELETE FROM companies WHERE company_id = %s", (company_id,))


class AtomicClient(FakeClient):
    """Serve one offline filing with real parser fixtures."""

    def __init__(self, case: SimpleNamespace, form: str, raw: str) -> None:
        self.case = case
        self.raw = raw
        self.form = form

    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        return {self.case.ticker: {"cik": self.case.cik, "name": self.case.ticker}}

    def submissions(self, cik: str) -> list[Submission]:
        return [
            Submission(
                accession=self.case.accession,
                form_type=self.form,
                filed_at="2026-08-30",
                period_of_report="2026-06-30",
                primary_document="report.xml",
            )
        ]

    def filing_index(self, cik: str, accession: str) -> list[dict[str, str]]:
        return [{"type": "xml", "name": "infotable.xml"}]

    def download(self, cik: str, accession: str, document: str) -> str:
        return self.raw

    def companyfacts(self, cik: str) -> dict[str, object]:
        return {"facts": {}}


def atomic_client(case: SimpleNamespace, form: str, *, empty: bool = False) -> AtomicClient:
    """Build a filing containing two transactions and a holding, or two 13F rows."""
    if form == "4":
        raw = (
            "<ownershipDocument/>"
            if empty
            else (Path(__file__).parent / "fixtures/sec/ownership_form4.xml").read_text()
        )
    elif form == "13F-HR":
        raw = (
            "<informationTable/>"
            if empty
            else (Path(__file__).parent / "fixtures/sec/form13f_infotable.xml").read_text()
        )
    else:
        raw = "<html><body></body></html>" if empty else FakeClient().download("", "", "")
    return AtomicClient(case, form, raw)


async def fetch_atomic(case: SimpleNamespace, client: AtomicClient) -> dict[str, int]:
    """Run the public incremental path with one issuer."""
    return await run_daily_fetch(
        case.writer,
        client,
        run_date="2026-08-31",
        root=case.root,
        universe=pl.DataFrame({"ticker": [case.ticker]}),
    )


async def child_counts(case: SimpleNamespace) -> list[int]:
    """Read all child tables from the independent observer."""
    counts = []
    for table in ("filing_text", "insider_transactions", "insider_holdings", "form13f_holdings"):
        row = await (
            await case.observer.execute(
                f"SELECT COUNT(*) FROM {table} JOIN filings USING (filing_id) WHERE accession = %s",
                (case.accession,),
            )
        ).fetchone()
        counts.append(row[0])
    return counts


@pytest.mark.parametrize("form", ["10-K", "8-K", "4", "13F-HR"])
@pytest.mark.parametrize("failure", ["before", "after", "parse"])
async def test_atomic_filing_failure_and_daily_retry(
    durable_filing, monkeypatch, form: str, failure: str
) -> None:
    """A failed parse or child persistence leaves no durable skip marker or children."""
    case = durable_filing
    client = atomic_client(case, form)
    child_name = {"4": "insert_insider_rows", "13F-HR": "insert_13f_holdings"}.get(
        form, "insert_filing_text"
    )
    parser_name = {"4": "parse_ownership_xml", "13F-HR": "parse_13f_infotable"}.get(
        form, "html_to_text"
    )
    real_child = getattr(fetch, child_name)

    async def fail_child(conn: psycopg.AsyncConnection, **kwargs) -> None:
        if failure == "after":
            await real_child(conn, **kwargs)
            # The real inserts succeeded, but must still be invisible outside.
            assert (await child_counts(case)) == [0, 0, 0, 0]
        raise RuntimeError("injected child failure")

    def fail_parse(*args, **kwargs) -> None:
        raise ValueError("injected parse failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            fetch,
            parser_name if failure == "parse" else child_name,
            fail_parse if failure == "parse" else fail_child,
        )
        failed = await fetch_atomic(case, client)
    assert failed["filings_skipped"] == 1
    assert failed["filings_parsed"] == 0
    assert failed["insider_rows"] == failed["form13f_rows"] == 0
    assert not await fetch.filing_exists(case.observer, accession=case.accession)
    assert await child_counts(case) == [0, 0, 0, 0]
    assert await get_checkpoint_date(case.observer, source="submissions", key=case.ticker) is None

    recovered = await fetch_atomic(case, client)
    assert recovered["filings_parsed"] == 1
    assert recovered["filings_skipped"] == 0
    assert await fetch.filing_exists(case.observer, accession=case.accession)
    expected = {"4": [0, 2, 1, 0], "13F-HR": [0, 0, 0, 2]}.get(form, [1, 0, 0, 0])
    assert await child_counts(case) == expected
    assert recovered["insider_rows"] == sum(expected[1:3])
    assert recovered["form13f_rows"] == expected[3]
    assert await get_checkpoint_date(case.observer, source="submissions", key=case.ticker) == (
        dt.date(2026, 8, 31)
    )
    assert (await fetch_atomic(case, client))["filings_fetched"] == 0
    assert await child_counts(case) == expected


async def test_atomic_child_constraint_failure_and_daily_retry(durable_filing, monkeypatch) -> None:
    """A PostgreSQL child constraint failure rolls back and leaves the connection usable."""
    case = durable_filing
    client = atomic_client(case, "8-K")
    real_insert = fetch.insert_filing_text
    errors = []

    async def fail_child(conn: psycopg.AsyncConnection, **kwargs) -> None:
        await real_insert(conn, **kwargs)
        row = await (
            await conn.execute(
                "SELECT COUNT(*) FROM filing_text WHERE filing_id = %s",
                (kwargs["filing_id"],),
            )
        ).fetchone()
        assert row[0] == 1
        with pytest.raises(psycopg.errors.NotNullViolation) as error:
            await conn.execute(
                "UPDATE filing_text SET body = %s WHERE filing_id = %s",
                (None, kwargs["filing_id"]),
            )
        errors.append(error.value)
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR
        raise error.value

    with monkeypatch.context() as patch:
        patch.setattr(fetch, "insert_filing_text", fail_child)
        failed = await fetch_atomic(case, client)
    assert len(errors) == 1
    assert failed["filings_skipped"] == 1
    assert failed["filings_parsed"] == 0
    assert (await (await case.writer.execute("SELECT 1")).fetchone())[0] == 1
    assert not await fetch.filing_exists(case.observer, accession=case.accession)
    assert await child_counts(case) == [0, 0, 0, 0]
    assert await get_checkpoint_date(case.observer, source="submissions", key=case.ticker) is None

    recovered = await fetch_atomic(case, client)
    assert recovered["filings_parsed"] == 1
    assert recovered["filings_skipped"] == 0
    assert await fetch.filing_exists(case.observer, accession=case.accession)
    assert await child_counts(case) == [1, 0, 0, 0]


async def test_atomic_failure_and_retry_preserve_outer_transaction(durable_filing, monkeypatch):
    """Filing rollback preserves unrelated writes and the caller's commit boundary."""
    case = durable_filing
    client = atomic_client(case, "8-K")
    real_insert = fetch.insert_filing_text
    updated_name = f"{case.ticker} updated"

    async def company_name(conn: psycopg.AsyncConnection) -> str:
        row = await (
            await conn.execute(
                "SELECT name FROM companies WHERE company_id = %s", (case.company_id,)
            )
        ).fetchone()
        return row[0]

    async def fail_child(conn: psycopg.AsyncConnection, **kwargs) -> None:
        await real_insert(conn, **kwargs)
        raise RuntimeError("child persistence failed")

    async with case.writer.transaction():
        await case.writer.execute(
            "UPDATE companies SET name = %s WHERE company_id = %s",
            (updated_name, case.company_id),
        )
        with monkeypatch.context() as patch:
            patch.setattr(fetch, "insert_filing_text", fail_child)
            failed = await fetch_atomic(case, client)
        assert failed["filings_skipped"] == 1
        assert failed["filings_parsed"] == 0
        assert await company_name(case.writer) == updated_name
        assert await company_name(case.observer) == case.ticker
        assert not await fetch.filing_exists(case.writer, accession=case.accession)
        assert not await fetch.filing_exists(case.observer, accession=case.accession)
        assert await child_counts(case) == [0, 0, 0, 0]

        recovered = await fetch_atomic(case, client)
        assert recovered["filings_parsed"] == 1
        assert recovered["filings_skipped"] == 0
        assert await fetch.filing_exists(case.writer, accession=case.accession)
        assert await company_name(case.writer) == updated_name
        assert await company_name(case.observer) == case.ticker
        assert not await fetch.filing_exists(case.observer, accession=case.accession)
        assert await child_counts(case) == [0, 0, 0, 0]
        assert (
            await get_checkpoint_date(case.observer, source="submissions", key=case.ticker) is None
        )

    assert await company_name(case.observer) == updated_name
    assert await fetch.filing_exists(case.observer, accession=case.accession)
    assert await child_counts(case) == [1, 0, 0, 0]
    assert await get_checkpoint_date(case.observer, source="submissions", key=case.ticker) == (
        dt.date(2026, 8, 31)
    )


@pytest.mark.parametrize("form", ["4", "13F-HR"])
async def test_atomic_partial_children_rollback(durable_filing, monkeypatch, form: str) -> None:
    """A real subset insert followed by failure must roll back with its parent."""
    case = durable_filing
    client = atomic_client(case, form)
    name = "insert_insider_rows" if form == "4" else "insert_13f_holdings"
    real_insert = getattr(fetch, name)

    async def partial_insert(conn: psycopg.AsyncConnection, **kwargs) -> None:
        if form == "4":
            kwargs["transactions"] = kwargs["transactions"][:1]
            kwargs["holdings"] = []
        else:
            kwargs["frame"] = kwargs["frame"].head(1)
        await real_insert(conn, **kwargs)
        raise RuntimeError("failure after first child")

    with monkeypatch.context() as patch:
        patch.setattr(fetch, name, partial_insert)
        assert (await fetch_atomic(case, client))["filings_skipped"] == 1
    assert not await fetch.filing_exists(case.observer, accession=case.accession)
    assert await child_counts(case) == [0, 0, 0, 0]
    assert (await fetch_atomic(case, client))["filings_parsed"] == 1
    assert await child_counts(case) == ([0, 2, 1, 0] if form == "4" else [0, 0, 0, 2])


@pytest.mark.parametrize("form", ["10-K", "8-K", "4", "13F-HR"])
async def test_atomic_empty_parse_commits(durable_filing, form: str) -> None:
    """Valid empty content is a completed filing and remains idempotent."""
    case = durable_filing
    client = atomic_client(case, form, empty=True)
    counts = await fetch_atomic(case, client)
    assert counts["filings_parsed"] == 1
    assert counts["filings_skipped"] == 0
    assert counts["insider_rows"] == counts["form13f_rows"] == 0
    assert await fetch.filing_exists(case.observer, accession=case.accession)
    assert await child_counts(case) == ([0, 0, 0, 0] if form in {"4", "13F-HR"} else [1, 0, 0, 0])
    assert await get_checkpoint_date(case.observer, source="submissions", key=case.ticker) == (
        dt.date(2026, 8, 31)
    )
    assert (await fetch_atomic(case, client))["filings_fetched"] == 0
