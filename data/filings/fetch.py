"""Daily incremental SEC filings fetch — discovery, download, parse, store.

Document filings (10-K/Q/8-K/20-F/6-K) and metadata-only filings (e.g.
DEF 14A) are dual-written: to DuckDB exactly as before (still the store
that queues filings for fact extraction via create_filing_article, out of
this cutover's scope) and, additively, to the new PostgreSQL `filings`/
`filing_text` tables via data.db.filings, keyed by company_id rather than
a raw ticker/CIK column. Ownership (forms 3/4/5) and 13F filings stay on
DuckDB only -- see docs/superpowers/plans/2026-08-11-postgres-filings-cutover.md
for the follow-up that finishes that cutover and drops the DuckDB writes
for the document/metadata path once it is safe to.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from pathlib import Path

import duckdb
import polars as pl
import psycopg
from loguru import logger

from data.db.filings import create_filing as pg_create_filing
from data.db.filings import insert_filing_text as pg_insert_filing_text
from data.db.filings import mark_checkpoint as pg_mark_checkpoint
from data.db.identifiers import get_current_company_id
from data.db.init import init_db
from data.db.pool import close_pool, open_pool
from data.filings.checkpoint import mark_checkpoint
from data.filings.cik import cik_for_ticker, ensure_cik_map
from data.filings.parse.form13f import parse_13f_infotable
from data.filings.parse.html import html_to_text
from data.filings.parse.ownership import parse_ownership_xml
from data.filings.store import (
    COVERED_FORMS,
    append_13f_holdings,
    create_filing_article,
    filing_exists,
    insert_13f_filing,
    insert_filing,
    insert_filing_text,
    insert_insider_rows,
    log_run,
    sha256_hex,
)
from data.sources.sec import EdgardClient, Submission
from data.universe.loader import get_universe

DOCUMENT_FORMS = {"10-K", "10-Q", "8-K", "20-F", "6-K"}
OWNERSHIP_FORMS = {"3", "4", "5"}
ROOT_DIR = Path(__file__).resolve().parents[2]

# (tickers_done, tickers_total, running counts)
ProgressCallback = Callable[[int, int, dict[str, int]], None]


def _info_table_file(client: EdgardClient, cik: str, accession: str) -> str | None:
    """Return the infotable XML filename in a filing directory, if any."""
    for item in client.filing_index(cik, accession):
        if item["type"] == "xml" and "infotable" in item["name"].lower():
            return item["name"]
    return None


def _edgar_url(cik: str, accession: str, doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"


def _ownership_xml_document(
    client: EdgardClient, cik: str, accession: str, primary_document: str
) -> str:
    """Resolve the raw ownership XML document name for a filing.

    EDGAR's submissions primaryDocument sometimes points at a stylesheet
    subdirectory (e.g. 'xslF345X06/form4.xml'), which serves the rendered HTML
    rather than the raw XML. The raw XML lives at the archive root; prefer a
    root-level .xml file from the filing index and fall back to the basename.
    """
    for item in client.filing_index(cik, accession):
        name = item["name"].lower()
        if item["type"] == "xml" and name.endswith(".xml") and not name.startswith("index"):
            return item["name"]
    return primary_document.rsplit("/", 1)[-1]


async def _process_document(
    conn: duckdb.DuckDBPyConnection,
    pg_conn: psycopg.AsyncConnection,
    client: EdgardClient,
    root: Path,
    *,
    cik: str,
    company_id: uuid.UUID | None,
    ticker: str,
    sub: Submission,
) -> str:
    """Download, store, and queue a full-text filing (10-K/Q/8-K/20-F/6-K).

    Writes to DuckDB (unchanged, including fact-extraction queueing) and,
    additively, to PostgreSQL. Returns the DuckDB filing id, which remains
    the id downstream DuckDB-side callers key off of.
    """
    raw = client.download(cik, sub.accession, sub.primary_document)
    text = html_to_text(raw)
    url = _edgar_url(cik, sub.accession, sub.primary_document)

    raw_path = root / "filings" / cik / f"{sub.accession}.html"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(raw, encoding="utf-8")
    text_path = root / "filings" / "text" / cik / f"{sub.accession}.txt"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")

    filing_id = insert_filing(
        conn,
        cik=cik,
        accession_number=sub.accession,
        form_type=sub.form_type,
        filed_at=sub.filed_at,
        period_of_report=sub.period_of_report,
        primary_document=sub.primary_document,
        url=url,
        text_path=str(text_path),
        text_hash=sha256_hex(text),
        size_bytes=len(raw.encode("utf-8")),
        ticker=ticker,
    )
    create_filing_article(
        conn,
        filing_id=filing_id,
        ticker=ticker,
        form_type=sub.form_type,
        filed_at=sub.filed_at,
        url=url,
        text_hash=sha256_hex(text),
    )
    insert_filing_text(conn, filing_id, text)

    pg_filing_id = await pg_create_filing(
        pg_conn,
        company_id=company_id,
        accession=sub.accession,
        form_type=sub.form_type,
        filed_at=dt.date.fromisoformat(sub.filed_at),
        period=dt.date.fromisoformat(sub.period_of_report) if sub.period_of_report else None,
        url=url,
    )
    await pg_insert_filing_text(pg_conn, filing_id=pg_filing_id, body=text)

    return filing_id


def _process_ownership(
    conn: duckdb.DuckDBPyConnection,
    client: EdgardClient,
    root: Path,
    *,
    cik: str,
    ticker: str,
    sub: Submission,
) -> tuple[int, int]:
    """Download and parse an insider ownership filing (forms 3/4/5).

    DuckDB only -- ownership is out of this cutover's scope.
    """
    document = _ownership_xml_document(client, cik, sub.accession, sub.primary_document)
    raw = client.download(cik, sub.accession, document)
    xml_path = root / "filings" / "ownership" / f"{sub.accession}.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(raw, encoding="utf-8")

    transactions, holdings = parse_ownership_xml(raw, ticker=ticker)
    filing_id = insert_filing(
        conn,
        cik=cik,
        accession_number=sub.accession,
        form_type=sub.form_type,
        filed_at=sub.filed_at,
        period_of_report=sub.period_of_report,
        primary_document=document,
        url=_edgar_url(cik, sub.accession, document),
        text_path=str(xml_path),
        text_hash=sha256_hex(raw),
        size_bytes=len(raw.encode("utf-8")),
        ticker=ticker,
    )
    return insert_insider_rows(conn, filing_id, transactions, holdings)


def _process_13f(
    conn: duckdb.DuckDBPyConnection,
    client: EdgardClient,
    root: Path,
    *,
    cik: str,
    ticker: str,
    sub: Submission,
) -> tuple[float | None, int]:
    """Download and parse a 13F-HR infotable; returns (total_value, n_rows).

    DuckDB (+ Parquet) only -- 13F is out of this cutover's scope.
    """
    info_table = _info_table_file(client, cik, sub.accession)
    if info_table is None:
        logger.warning(f"13F filing {sub.accession} has no infotable XML; skipped")
        return None, 0

    raw = client.download(cik, sub.accession, info_table)
    period = sub.period_of_report or sub.filed_at
    df = parse_13f_infotable(raw, report_period=period, filer_cik=cik, filer_name=None)
    filing_id = insert_filing(
        conn,
        cik=cik,
        accession_number=sub.accession,
        form_type=sub.form_type,
        filed_at=sub.filed_at,
        period_of_report=sub.period_of_report,
        primary_document=info_table,
        url=_edgar_url(cik, sub.accession, info_table),
        text_hash=sha256_hex(raw),
        size_bytes=len(raw.encode("utf-8")),
        ticker=ticker,
    )
    total = df["value_usd"].sum() if not df.is_empty() else None
    insert_13f_filing(
        conn,
        filing_id=filing_id,
        filer_cik=cik,
        filer_name=None,
        report_period=period,
        total_value=float(total) if total is not None else None,
    )
    append_13f_holdings(df, root=root, report_period=period)
    return total, df.height


async def _process_metadata_only(
    conn: duckdb.DuckDBPyConnection,
    pg_conn: psycopg.AsyncConnection,
    *,
    cik: str,
    company_id: uuid.UUID | None,
    ticker: str,
    sub: Submission,
) -> str:
    """Insert a filing metadata row without downloading full text (e.g. DEF 14A).

    Dual-written to DuckDB and PostgreSQL, same as _process_document.
    """
    url = _edgar_url(cik, sub.accession, sub.primary_document)
    filing_id = insert_filing(
        conn,
        cik=cik,
        accession_number=sub.accession,
        form_type=sub.form_type,
        filed_at=sub.filed_at,
        period_of_report=sub.period_of_report,
        primary_document=sub.primary_document,
        url=url,
        ticker=ticker,
    )
    await pg_create_filing(
        pg_conn,
        company_id=company_id,
        accession=sub.accession,
        form_type=sub.form_type,
        filed_at=dt.date.fromisoformat(sub.filed_at),
        period=dt.date.fromisoformat(sub.period_of_report) if sub.period_of_report else None,
        url=url,
    )
    return filing_id


async def run_daily_fetch(
    conn: duckdb.DuckDBPyConnection,
    pg_conn: psycopg.AsyncConnection,
    client: EdgardClient,
    *,
    run_date: str,
    root: Path,
    universe: pl.DataFrame,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Fetch new SEC filings for the universe and store them.

    Args:
        conn: DuckDB connection -- still the dedup gate (filing_exists) and
            the sole store for ownership/13F filings.
        pg_conn: PostgreSQL connection -- additive store for document and
            metadata-only filings (data.db.filings).
        client: EDGAR client.
        run_date: Run date (YYYY-MM-DD).
        root: Repository root for raw storage.
        universe: DataFrame with a ticker column.
        progress: Optional callable(tickers_done, tickers_total, counts)
            invoked after each ticker, so a caller can report live progress.

    Returns:
        Counts: filings_fetched, filings_parsed, filings_skipped,
        insider_rows, form13f_rows.
    """
    tickers = sorted(universe["ticker"].unique().to_list())
    ensure_cik_map(conn, client, tickers)
    counts = {
        "filings_fetched": 0,
        "filings_parsed": 0,
        "filings_skipped": 0,
        "insider_rows": 0,
        "form13f_rows": 0,
    }
    started = dt.datetime.now(dt.UTC)

    for done, ticker in enumerate(tickers, start=1):
        cik = cik_for_ticker(conn, ticker)
        if cik is None:
            logger.warning(f"no SEC CIK for {ticker}; skipped")
            counts["filings_skipped"] += 1
            if progress is not None:
                progress(done, len(tickers), counts)
            continue
        company_id = await get_current_company_id(pg_conn, id_type="ticker", id_value=ticker)
        for sub in client.submissions(cik):
            if sub.form_type not in COVERED_FORMS or filing_exists(conn, cik, sub.accession):
                continue
            counts["filings_fetched"] += 1
            try:
                if sub.form_type in DOCUMENT_FORMS:
                    await _process_document(
                        conn,
                        pg_conn,
                        client,
                        root,
                        cik=cik,
                        company_id=company_id,
                        ticker=ticker,
                        sub=sub,
                    )
                elif sub.form_type in OWNERSHIP_FORMS:
                    n_tx, n_h = _process_ownership(
                        conn, client, root, cik=cik, ticker=ticker, sub=sub
                    )
                    counts["insider_rows"] += n_tx + n_h
                elif sub.form_type.startswith("13F"):
                    _, n_rows = _process_13f(conn, client, root, cik=cik, ticker=ticker, sub=sub)
                    counts["form13f_rows"] += n_rows
                else:
                    await _process_metadata_only(
                        conn, pg_conn, cik=cik, company_id=company_id, ticker=ticker, sub=sub
                    )
                counts["filings_parsed"] += 1
            except Exception:
                logger.exception(f"filing {sub.accession} for {ticker} failed; skipped")
                counts["filings_skipped"] += 1
        mark_checkpoint(conn, f"submissions:{ticker}", run_date)
        await pg_mark_checkpoint(pg_conn, source=f"submissions:{ticker}", key=run_date)
        if progress is not None:
            progress(done, len(tickers), counts)

    log_run(conn, job_type="filings", run_date=run_date, started_at=started, **counts)
    return counts


async def run_daily_fetch_with_pool(
    conn: duckdb.DuckDBPyConnection,
    client: EdgardClient,
    *,
    run_date: str,
    root: Path,
    universe: pl.DataFrame,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Open a Postgres pool for the duration of one run_daily_fetch call.

    Bridges the async repository layer into fetch.py's synchronous CLI
    entry point (main()) and, indirectly, into the thread-based
    server.jobs.start_run work callback used by the run_filings_refresh
    MCP tool -- asyncio.run() at that boundary is an acceptable bridge for
    now (server/jobs.py itself is unaffected by this cutover).
    """
    pool = await open_pool()
    try:
        async with pool.connection() as pg_conn:
            return await run_daily_fetch(
                conn,
                pg_conn,
                client,
                run_date=run_date,
                root=root,
                universe=universe,
                progress=progress,
            )
    finally:
        await close_pool(pool)


def main() -> None:
    """Run the daily filings fetch for today's universe."""
    import asyncio

    conn = init_db()
    try:
        today = dt.date.today().isoformat()
        universe = get_universe(today)
        counts = asyncio.run(
            run_daily_fetch_with_pool(
                conn,
                EdgardClient(),
                run_date=today,
                root=ROOT_DIR,
                universe=universe,
            )
        )
        logger.info(f"filings fetch complete: {counts}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
