"""One-time SEC filings backfill backed exclusively by PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import calendar
import datetime as dt
from pathlib import Path

import psycopg
from loguru import logger

from data.db.filings import create_filing, filing_exists, has_checkpoint, mark_checkpoint
from data.db.identifiers import get_current_company_id
from data.db.pool import close_pool, open_pool
from data.filings.cik import cik_for_ticker, ensure_cik_map
from data.filings.companyfacts import ingest_companyfacts
from data.filings.fetch import (
    DOCUMENT_FORMS,
    OWNERSHIP_FORMS,
    ROOT_DIR,
    _edgar_url,
    _process_13f,
    _process_document,
    _process_ownership,
)
from data.sources.sec import EdgardClient, Submission
from data.universe.loader import get_universe

FULL_TEXT_YEARS = 5


def _quarter_end(date_str: str) -> str:
    """Return the calendar quarter-end containing a date."""
    year, month = int(date_str[:4]), int(date_str[5:7])
    end_month = ((month - 1) // 3) * 3 + 3
    return f"{year}-{end_month:02d}-{calendar.monthrange(year, end_month)[1]}"


async def backfill_issuer(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    root: Path,
    *,
    ticker: str,
    cik: str,
    full_text_since: str,
    checkpoint_date: str | None = None,
) -> dict[str, int]:
    """Backfill one issuer's documents, ownership forms, and 13F rows."""
    counts = {
        "filings_fetched": 0,
        "filings_parsed": 0,
        "filings_skipped": 0,
        "insider_rows": 0,
        "form13f_rows": 0,
    }
    company_id = await get_current_company_id(conn, id_type="ticker", id_value=ticker)
    for sub in client.all_submissions(cik):
        if await filing_exists(conn, accession=sub.accession):
            continue
        counts["filings_fetched"] += 1
        try:
            if sub.form_type in DOCUMENT_FORMS and sub.filed_at >= full_text_since:
                await _process_document(
                    conn, client, root, cik=cik, company_id=company_id, ticker=ticker, sub=sub
                )
            elif sub.form_type in OWNERSHIP_FORMS and company_id is not None:
                tx_count, holding_count = await _process_ownership(
                    conn, client, root, cik=cik, company_id=company_id, ticker=ticker, sub=sub
                )
                counts["insider_rows"] += tx_count + holding_count
            elif sub.form_type.startswith("13F"):
                counts["form13f_rows"] += await _process_13f(
                    conn, client, root, cik=cik, company_id=company_id, sub=sub
                )
            else:
                await create_filing(
                    conn,
                    company_id=company_id,
                    accession=sub.accession,
                    form_type=sub.form_type,
                    filed_at=dt.date.fromisoformat(sub.filed_at),
                    period=(
                        dt.date.fromisoformat(sub.period_of_report)
                        if sub.period_of_report
                        else None
                    ),
                    url=_edgar_url(cik, sub.accession, sub.primary_document),
                )
            counts["filings_parsed"] += 1
        except Exception:
            logger.exception(f"backfill: filing {sub.accession} for {ticker} failed")
            counts["filings_skipped"] += 1
    await mark_checkpoint(
        conn,
        source=f"backfill:issuer:{ticker}",
        key=checkpoint_date or dt.date.today().isoformat(),
    )
    return counts


def iter_13f_filings(client: EdgardClient, *, start: str, end: str):
    """Yield indexed 13F filing metadata for a date range."""
    cursor: str | None = None
    while True:
        payload = client.search_index(
            query="13F-HR", forms="13F-HR", start=start, end=end, cursor=cursor
        )
        hits = payload["hits"]["hits"]
        for hit in hits:
            source = hit["_source"]
            yield (
                source["cik"],
                source.get("accessionNumber", "").replace("-", ""),
                source.get("filingDate"),
                source.get("entityName"),
            )
        cursor = payload.get("cursor")
        if not hits or not cursor:
            break


async def backfill_13f(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    root: Path,
    *,
    start: str,
    end: str,
) -> dict[str, int]:
    """Backfill indexed 13F filings and holdings resumably."""
    counts = {"13f_filings": 0, "13f_rows": 0, "skipped": 0}
    for cik, accession, filed_at, filer_name in iter_13f_filings(client, start=start, end=end):
        if await filing_exists(conn, accession=accession) or await has_checkpoint(
            conn, source="13f", key=accession
        ):
            continue
        try:
            sub = Submission(
                accession=accession,
                form_type="13F-HR",
                filed_at=filed_at,
                period_of_report=_quarter_end(filed_at),
                primary_document="",
            )
            rows = await _process_13f(
                conn,
                client,
                root,
                cik=cik,
                company_id=None,
                sub=sub,
                filer_name=filer_name,
            )
            if rows:
                counts["13f_filings"] += 1
                counts["13f_rows"] += rows
            else:
                counts["skipped"] += 1
        except Exception:
            logger.exception(f"backfill: 13F filing {accession} failed")
            counts["skipped"] += 1
        await mark_checkpoint(conn, source="13f", key=accession)
    return counts


async def backfill_financials(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    *,
    tickers: list[str],
) -> dict[str, int]:
    """Backfill complete Company Facts histories for tracked SEC issuers.

    Args:
        conn: An open PostgreSQL connection.
        client: Courtesy-limited SEC client.
        tickers: Current universe tickers to resolve to companies and CIKs.

    Returns:
        Counts of issuers processed, newly inserted facts, and skipped issuers.
    """
    await ensure_cik_map(conn, client, tickers)
    counts = {"financial_issuers": 0, "financial_facts": 0, "financials_skipped": 0}
    for ticker in tickers:
        cik = await cik_for_ticker(conn, ticker)
        company_id = await get_current_company_id(conn, id_type="ticker", id_value=ticker)
        if cik is None or company_id is None:
            counts["financials_skipped"] += 1
            continue
        try:
            counts["financial_facts"] += await ingest_companyfacts(
                conn, client, company_id=company_id, cik=cik
            )
            counts["financial_issuers"] += 1
        except Exception:
            logger.exception(f"backfill: company facts for {ticker} failed")
            counts["financials_skipped"] += 1
    return counts


async def _run(args: argparse.Namespace) -> None:
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            client = EdgardClient()
            tickers: list[str] = []
            if args.issuers or args.financials:
                today = dt.date.today().isoformat()
                tickers = sorted(get_universe(today)["ticker"].unique().to_list())
                await ensure_cik_map(conn, client, tickers)
            if args.issuers:
                full_text_since = str(
                    dt.date.today().replace(year=dt.date.today().year - FULL_TEXT_YEARS)
                )
                for ticker in tickers:
                    cik = await cik_for_ticker(conn, ticker)
                    if cik:
                        await backfill_issuer(
                            conn,
                            client,
                            ROOT_DIR,
                            ticker=ticker,
                            cik=cik,
                            full_text_since=full_text_since,
                        )
            if args.financials:
                counts = await backfill_financials(conn, client, tickers=tickers)
                logger.info(f"company facts backfill complete: {counts}")
            if not args.financials:
                await backfill_13f(
                    conn,
                    client,
                    ROOT_DIR,
                    start=args.backfill_13f_start,
                    end=args.backfill_13f_end,
                )
    finally:
        await close_pool(pool)


def main() -> None:
    """Run issuer and/or 13F backfill from the command line."""
    parser = argparse.ArgumentParser(description="SEC filings backfill")
    parser.add_argument("--issuers", action="store_true")
    parser.add_argument(
        "--financials", action="store_true", help="backfill SEC Company Facts histories"
    )
    parser.add_argument("--13f-start", dest="backfill_13f_start", default="1998-01-01")
    parser.add_argument("--13f-end", dest="backfill_13f_end", default=dt.date.today().isoformat())
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
