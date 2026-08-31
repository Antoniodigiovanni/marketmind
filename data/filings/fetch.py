"""Daily incremental SEC filings fetch backed exclusively by PostgreSQL."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path

import polars as pl
import psycopg
from loguru import logger

from data.db.filings import (
    create_filing,
    filing_exists,
    get_checkpoint_date,
    insert_13f_holdings,
    insert_filing_text,
    insert_insider_rows,
    mark_checkpoint,
)
from data.db.identifiers import get_current_company_id
from data.db.pool import close_pool, open_pool
from data.db.runs import finish_run, start_run
from data.filings.cik import cik_for_ticker, ensure_cik_map
from data.filings.companyfacts import ingest_companyfacts
from data.filings.parse.form13f import parse_13f_infotable
from data.filings.parse.html import html_to_text
from data.filings.parse.ownership import parse_ownership_xml
from data.sources.sec import EdgardClient, Submission
from data.universe.loader import get_universe

DOCUMENT_FORMS = {"10-K", "10-Q", "8-K", "20-F", "6-K"}
OWNERSHIP_FORMS = {"3", "4", "5"}
COVERED_FORMS = DOCUMENT_FORMS | OWNERSHIP_FORMS | {"13F-HR", "13F-HR/A", "DEF 14A"}
INITIAL_LOOKBACK_DAYS = 7
ROOT_DIR = Path(__file__).resolve().parents[2]
ProgressCallback = Callable[[int, int, dict[str, int]], None]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _info_table_file(client: EdgardClient, cik: str, accession: str) -> str | None:
    """Return the infotable XML filename in a filing directory, if any."""
    for item in client.filing_index(cik, accession):
        if item["type"] == "xml" and "infotable" in item["name"].lower():
            return item["name"]
    return None


def _edgar_url(cik: str, accession: str, document: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"


def _ownership_xml_document(
    client: EdgardClient, cik: str, accession: str, primary_document: str
) -> str:
    """Resolve the raw ownership XML filename for a filing."""
    for item in client.filing_index(cik, accession):
        name = item["name"].lower()
        if item["type"] == "xml" and name.endswith(".xml") and not name.startswith("index"):
            return item["name"]
    return primary_document.rsplit("/", 1)[-1]


async def _process_document(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    root: Path,
    *,
    cik: str,
    company_id: uuid.UUID | None,
    ticker: str,
    sub: Submission,
) -> uuid.UUID:
    """Download and store a full-text filing."""
    raw = client.download(cik, sub.accession, sub.primary_document)
    text = html_to_text(raw)
    raw_path = root / "filings" / cik / f"{sub.accession}.html"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(raw, encoding="utf-8")
    text_path = root / "filings" / "text" / cik / f"{sub.accession}.txt"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")
    filing_id = await create_filing(
        conn,
        company_id=company_id,
        accession=sub.accession,
        form_type=sub.form_type,
        filed_at=dt.date.fromisoformat(sub.filed_at),
        period=dt.date.fromisoformat(sub.period_of_report) if sub.period_of_report else None,
        url=_edgar_url(cik, sub.accession, sub.primary_document),
    )
    await insert_filing_text(conn, filing_id=filing_id, body=text)
    return filing_id


async def _process_ownership(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    root: Path,
    *,
    cik: str,
    company_id: uuid.UUID,
    ticker: str,
    sub: Submission,
) -> tuple[int, int]:
    """Download and store an insider ownership filing."""
    document = _ownership_xml_document(client, cik, sub.accession, sub.primary_document)
    raw = client.download(cik, sub.accession, document)
    path = root / "filings" / "ownership" / f"{sub.accession}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    filing_id = await create_filing(
        conn,
        company_id=company_id,
        accession=sub.accession,
        form_type=sub.form_type,
        filed_at=dt.date.fromisoformat(sub.filed_at),
        period=dt.date.fromisoformat(sub.period_of_report) if sub.period_of_report else None,
        url=_edgar_url(cik, sub.accession, document),
    )
    transactions, holdings = parse_ownership_xml(raw, ticker=ticker)
    return await insert_insider_rows(
        conn,
        filing_id=filing_id,
        company_id=company_id,
        transactions=transactions,
        holdings=holdings,
    )


async def _process_13f(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    root: Path,
    *,
    cik: str,
    company_id: uuid.UUID | None,
    sub: Submission,
    filer_name: str | None = None,
) -> int:
    """Download and store a 13F filing and its holdings."""
    info_table = _info_table_file(client, cik, sub.accession)
    if info_table is None:
        return 0
    raw = client.download(cik, sub.accession, info_table)
    path = root / "filings" / "13f" / f"{sub.accession}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    period = sub.period_of_report or sub.filed_at
    frame = parse_13f_infotable(raw, report_period=period, filer_cik=cik, filer_name=filer_name)
    filing_id = await create_filing(
        conn,
        company_id=company_id,
        accession=sub.accession,
        form_type=sub.form_type,
        filed_at=dt.date.fromisoformat(sub.filed_at),
        period=dt.date.fromisoformat(period),
        url=_edgar_url(cik, sub.accession, info_table),
    )
    return await insert_13f_holdings(conn, filing_id=filing_id, frame=frame)


async def _process_metadata_only(
    conn: psycopg.AsyncConnection,
    *,
    cik: str,
    company_id: uuid.UUID | None,
    sub: Submission,
) -> uuid.UUID:
    """Store filing metadata without downloading its document."""
    return await create_filing(
        conn,
        company_id=company_id,
        accession=sub.accession,
        form_type=sub.form_type,
        filed_at=dt.date.fromisoformat(sub.filed_at),
        period=dt.date.fromisoformat(sub.period_of_report) if sub.period_of_report else None,
        url=_edgar_url(cik, sub.accession, sub.primary_document),
    )


async def run_daily_fetch(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    *,
    run_date: str,
    root: Path,
    universe: pl.DataFrame,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Fetch new SEC filings for the universe into PostgreSQL.

    Each ticker overlaps its last completed date so late same-day filings are
    still discovered. A fresh database scans only the previous seven days;
    historical coverage belongs to the explicit backfill command.

    Company facts are refreshed per ticker alongside the filings. They are a
    separate concern from filing coverage, so a failed facts refresh is counted
    and logged but does not hold back the submissions checkpoint.

    Args:
        conn: An open PostgreSQL connection.
        client: SEC archive and submissions client.
        run_date: Inclusive fetch cutoff in YYYY-MM-DD format.
        root: Root directory for immutable filing archives.
        universe: Frame containing at least a ticker column.
        progress: Optional callback receiving completed tickers, total tickers,
            and the current counters.

    Returns:
        Filing, parse, skip, insider-row, 13F-row, and company-facts counters.

    Raises:
        ValueError: If run_date or a source filing date is invalid.
    """
    run_date_value = dt.date.fromisoformat(run_date)
    tickers = sorted(universe["ticker"].unique().to_list())
    await ensure_cik_map(conn, client, tickers)
    counts = {
        "filings_fetched": 0,
        "filings_parsed": 0,
        "filings_skipped": 0,
        "insider_rows": 0,
        "form13f_rows": 0,
        "financial_facts": 0,
        "financial_issuers": 0,
        "financials_skipped": 0,
    }
    run_id = await start_run(conn, run_date=run_date_value, job_type="filings")
    try:
        for done, ticker in enumerate(tickers, start=1):
            cik = await cik_for_ticker(conn, ticker)
            company_id = await get_current_company_id(conn, id_type="ticker", id_value=ticker)
            if cik is None:
                counts["filings_skipped"] += 1
                continue
            checkpoint = await get_checkpoint_date(conn, source="submissions", key=ticker)
            filed_from = checkpoint or (run_date_value - dt.timedelta(days=INITIAL_LOOKBACK_DAYS))
            ticker_failed = False
            for sub in client.submissions(cik):
                filed_at = dt.date.fromisoformat(sub.filed_at)
                if filed_at < filed_from or filed_at > run_date_value:
                    continue
                if sub.form_type not in COVERED_FORMS or await filing_exists(
                    conn, accession=sub.accession
                ):
                    continue
                counts["filings_fetched"] += 1
                try:
                    if sub.form_type in DOCUMENT_FORMS:
                        await _process_document(
                            conn,
                            client,
                            root,
                            cik=cik,
                            company_id=company_id,
                            ticker=ticker,
                            sub=sub,
                        )
                    elif sub.form_type in OWNERSHIP_FORMS and company_id is not None:
                        tx_count, holding_count = await _process_ownership(
                            conn,
                            client,
                            root,
                            cik=cik,
                            company_id=company_id,
                            ticker=ticker,
                            sub=sub,
                        )
                        counts["insider_rows"] += tx_count + holding_count
                    elif sub.form_type.startswith("13F"):
                        counts["form13f_rows"] += await _process_13f(
                            conn, client, root, cik=cik, company_id=company_id, sub=sub
                        )
                    else:
                        await _process_metadata_only(conn, cik=cik, company_id=company_id, sub=sub)
                    counts["filings_parsed"] += 1
                except Exception:
                    logger.exception(f"filing {sub.accession} for {ticker} failed; skipped")
                    counts["filings_skipped"] += 1
                    ticker_failed = True
            if company_id is not None:
                try:
                    counts["financial_facts"] += await ingest_companyfacts(
                        conn, client, company_id=company_id, cik=cik
                    )
                    counts["financial_issuers"] += 1
                except Exception:
                    logger.exception(f"company facts refresh for {ticker} failed; skipped")
                    counts["financials_skipped"] += 1
            if not ticker_failed:
                await mark_checkpoint(
                    conn,
                    source="submissions",
                    key=ticker,
                    last_date=run_date_value,
                )
            if progress is not None:
                progress(done, len(tickers), counts)
    except Exception as exc:
        await finish_run(conn, run_id=run_id, status="failed", counters=counts, error=str(exc))
        raise
    await finish_run(conn, run_id=run_id, status="succeeded", counters=counts)
    return counts


async def run_daily_fetch_with_pool(
    client: EdgardClient,
    *,
    run_date: str,
    root: Path,
    universe: pl.DataFrame,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Open a PostgreSQL pool and run the daily filings fetch."""
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            return await run_daily_fetch(
                conn,
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
    today = dt.date.today().isoformat()
    counts = asyncio.run(
        run_daily_fetch_with_pool(
            EdgardClient(), run_date=today, root=ROOT_DIR, universe=get_universe(today)
        )
    )
    logger.info(f"filings fetch complete: {counts}")


if __name__ == "__main__":
    main()
