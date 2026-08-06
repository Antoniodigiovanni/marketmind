"""One-time SEC filings backfill — deep metadata/tabular, 5-year full text."""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
from pathlib import Path

import duckdb
from loguru import logger

from data.db.init import init_db
from data.filings.checkpoint import has_checkpoint, mark_checkpoint
from data.filings.cik import cik_for_ticker, ensure_cik_map
from data.filings.fetch import (
    DOCUMENT_FORMS,
    OWNERSHIP_FORMS,
    ROOT_DIR,
    _edgar_url,
    _process_document,
    _process_ownership,
)
from data.filings.parse.form13f import parse_13f_cover, parse_13f_infotable
from data.filings.store import (
    COVERED_FORMS,
    append_13f_holdings,
    filing_exists,
    insert_13f_filing,
    insert_filing,
    sha256_hex,
)
from data.sources.sec import EdgardClient
from data.universe.loader import get_universe

FULL_TEXT_YEARS = 5


def _info_table_file(files: list[dict[str, str]]) -> str | None:
    """Return the infotable XML filename from an archive file list."""
    for item in files:
        if item["type"] == "xml" and "infotable" in item["name"].lower():
            return item["name"]
    return None


def _cover_file(files: list[dict[str, str]]) -> str | None:
    """Return the first non-infotable XML filename (the cover page)."""
    for item in files:
        if item["type"] == "xml" and "infotable" not in item["name"].lower():
            return item["name"]
    return None


def _quarter_end(date_str: str) -> str:
    """Return the calendar quarter-end date containing date_str."""
    year = int(date_str[:4])
    month = int(date_str[5:7])
    end_month = ((month - 1) // 3) * 3 + 3
    return f"{year}-{end_month:02d}-{calendar.monthrange(year, end_month)[1]}"


def backfill_issuer(
    conn: duckdb.DuckDBPyConnection,
    client: EdgardClient,
    root: Path,
    *,
    ticker: str,
    cik: str,
    full_text_since: str,
    checkpoint_date: str | None = None,
) -> dict[str, int]:
    """Backfill one issuer: deep metadata, 5-year full text, all insider forms.

    Args:
        conn: DuckDB connection.
        client: EDGAR client.
        root: Repository root for raw storage.
        ticker: Ticker symbol.
        cik: 10-digit issuer CIK.
        full_text_since: Only full-text documents filed on/after this date.
        checkpoint_date: Checkpoint key date; defaults to today.

    Returns:
        Counts: filings_fetched, filings_parsed, filings_skipped,
        insider_rows, form13f_rows.
    """
    counts = {
        "filings_fetched": 0,
        "filings_parsed": 0,
        "filings_skipped": 0,
        "insider_rows": 0,
        "form13f_rows": 0,
    }
    for sub in client.all_submissions(cik):
        if sub.form_type not in COVERED_FORMS or filing_exists(conn, cik, sub.accession):
            continue
        counts["filings_fetched"] += 1
        try:
            if sub.form_type in DOCUMENT_FORMS and sub.filed_at >= full_text_since:
                _process_document(conn, client, root, cik=cik, ticker=ticker, sub=sub)
            elif sub.form_type in OWNERSHIP_FORMS:
                n_tx, n_h = _process_ownership(conn, client, root, cik=cik, ticker=ticker, sub=sub)
                counts["insider_rows"] += n_tx + n_h
            elif sub.form_type.startswith("13F"):
                info_table = _info_table_file(client.filing_index(cik, sub.accession))
                if info_table is not None:
                    raw = client.download(cik, sub.accession, info_table)
                    period = sub.period_of_report or _quarter_end(sub.filed_at)
                    df = parse_13f_infotable(
                        raw, report_period=period, filer_cik=cik, filer_name=None
                    )
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
                    counts["form13f_rows"] += df.height
            else:
                # metadata only — older full-text documents are out of window
                insert_filing(
                    conn,
                    cik=cik,
                    accession_number=sub.accession,
                    form_type=sub.form_type,
                    filed_at=sub.filed_at,
                    period_of_report=sub.period_of_report,
                    primary_document=sub.primary_document,
                    url=_edgar_url(cik, sub.accession, sub.primary_document),
                    ticker=ticker,
                )
            counts["filings_parsed"] += 1
        except Exception:
            logger.exception(f"backfill: filing {sub.accession} for {ticker} failed; skipped")
            counts["filings_skipped"] += 1
    mark_checkpoint(
        conn,
        f"backfill:issuer:{ticker}",
        checkpoint_date or dt.date.today().isoformat(),
    )
    return counts


def iter_13f_filings(client: EdgardClient, *, start: str, end: str):
    """Yield (cik, accession, filed_at, filer_name) for 13F-HR filings."""
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


def backfill_13f(
    conn: duckdb.DuckDBPyConnection,
    client: EdgardClient,
    root: Path,
    *,
    start: str,
    end: str,
) -> dict[str, int]:
    """Backfill 13F-HR filings and holdings for a date range, resumably.

    Args:
        conn: DuckDB connection.
        client: EDGAR client.
        root: Repository root for raw storage.
        start: Filing-date range start (YYYY-MM-DD).
        end: Filing-date range end (YYYY-MM-DD).

    Returns:
        Counts: 13f_filings, 13f_rows, skipped.
    """
    counts = {"13f_filings": 0, "13f_rows": 0, "skipped": 0}
    for cik, accession, filed_at, filer_name in iter_13f_filings(client, start=start, end=end):
        if filing_exists(conn, cik, accession) or has_checkpoint(conn, "13f", accession):
            continue
        try:
            files = client.filing_index(cik, accession)
            info_table = _info_table_file(files)
            cover = _cover_file(files)
            if info_table is None:
                counts["skipped"] += 1
                mark_checkpoint(conn, "13f", accession)
                continue
            raw_infotable = client.download(cik, accession, info_table)
            raw_cover = client.download(cik, accession, cover) if cover else None
            period = parse_13f_cover(raw_cover) if raw_cover else _quarter_end(filed_at)
            df = parse_13f_infotable(
                raw_infotable, report_period=period, filer_cik=cik, filer_name=filer_name
            )
            filing_id = insert_filing(
                conn,
                cik=cik,
                accession_number=accession,
                form_type="13F-HR",
                filed_at=filed_at,
                period_of_report=period,
                primary_document=info_table,
                url=_edgar_url(cik, accession, info_table),
                text_hash=sha256_hex(raw_infotable),
                size_bytes=len(raw_infotable.encode("utf-8")),
            )
            total = df["value_usd"].sum() if not df.is_empty() else None
            insert_13f_filing(
                conn,
                filing_id=filing_id,
                filer_cik=cik,
                filer_name=filer_name,
                report_period=period,
                total_value=float(total) if total is not None else None,
            )
            append_13f_holdings(df, root=root, report_period=period)
            counts["13f_filings"] += 1
            counts["13f_rows"] += df.height
        except Exception:
            logger.exception(f"backfill: 13F filing {accession} failed; skipped")
            counts["skipped"] += 1
        mark_checkpoint(conn, "13f", accession)
    return counts


def main() -> None:
    """Run issuer and/or 13F backfill from the CLI."""
    parser = argparse.ArgumentParser(description="SEC filings backfill")
    parser.add_argument("--issuers", action="store_true", help="backfill universe issuers")
    parser.add_argument(
        "--13f-start",
        dest="backfill_13f_start",
        default="1998-01-01",
        help="13F backfill start date",
    )
    parser.add_argument(
        "--13f-end",
        dest="backfill_13f_end",
        default=dt.date.today().isoformat(),
        help="13F backfill end date",
    )
    args = parser.parse_args()

    conn = init_db()
    try:
        today = dt.date.today().isoformat()
        if args.issuers:
            universe = get_universe(today)
            tickers = sorted(universe["ticker"].unique().to_list())
            ensure_cik_map(conn, EdgardClient(), tickers)
            full_text_since = str(
                dt.date.today().replace(year=dt.date.today().year - FULL_TEXT_YEARS)
            )
            for ticker in tickers:
                cik = cik_for_ticker(conn, ticker)
                if cik is None:
                    continue
                backfill_issuer(
                    conn,
                    EdgardClient(),
                    ROOT_DIR,
                    ticker=ticker,
                    cik=cik,
                    full_text_since=full_text_since,
                )
        backfill_13f(
            conn,
            EdgardClient(),
            ROOT_DIR,
            start=args.backfill_13f_start,
            end=args.backfill_13f_end,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
