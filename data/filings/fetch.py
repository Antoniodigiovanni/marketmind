"""Daily incremental SEC filings fetch — discovery, download, parse, store."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import polars as pl
from loguru import logger

from data.db.init import init_db
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


def _process_document(
    conn: duckdb.DuckDBPyConnection,
    client: EdgardClient,
    root: Path,
    *,
    cik: str,
    ticker: str,
    sub: Submission,
) -> str:
    """Download, store, and queue a full-text filing (10-K/Q/8-K/20-F/6-K)."""
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
    """Download and parse an insider ownership filing (forms 3/4/5)."""
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
    """Download and parse a 13F-HR infotable; returns (total_value, n_rows)."""
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


def _process_metadata_only(
    conn: duckdb.DuckDBPyConnection, *, cik: str, ticker: str, sub: Submission
) -> str:
    """Insert a filing metadata row without downloading full text (e.g. DEF 14A)."""
    return insert_filing(
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


def run_daily_fetch(
    conn: duckdb.DuckDBPyConnection,
    client: EdgardClient,
    *,
    run_date: str,
    root: Path,
    universe: pl.DataFrame,
) -> dict[str, int]:
    """Fetch new SEC filings for the universe and store them.

    Args:
        conn: DuckDB connection.
        client: EDGAR client.
        run_date: Run date (YYYY-MM-DD).
        root: Repository root for raw storage.
        universe: DataFrame with a ticker column.

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

    for ticker in tickers:
        cik = cik_for_ticker(conn, ticker)
        if cik is None:
            logger.warning(f"no SEC CIK for {ticker}; skipped")
            counts["filings_skipped"] += 1
            continue
        for sub in client.submissions(cik):
            if sub.form_type not in COVERED_FORMS or filing_exists(conn, cik, sub.accession):
                continue
            counts["filings_fetched"] += 1
            try:
                if sub.form_type in DOCUMENT_FORMS:
                    _process_document(conn, client, root, cik=cik, ticker=ticker, sub=sub)
                elif sub.form_type in OWNERSHIP_FORMS:
                    n_tx, n_h = _process_ownership(
                        conn, client, root, cik=cik, ticker=ticker, sub=sub
                    )
                    counts["insider_rows"] += n_tx + n_h
                elif sub.form_type.startswith("13F"):
                    _, n_rows = _process_13f(conn, client, root, cik=cik, ticker=ticker, sub=sub)
                    counts["form13f_rows"] += n_rows
                else:
                    _process_metadata_only(conn, cik=cik, ticker=ticker, sub=sub)
                counts["filings_parsed"] += 1
            except Exception:
                logger.exception(f"filing {sub.accession} for {ticker} failed; skipped")
                counts["filings_skipped"] += 1
        mark_checkpoint(conn, f"submissions:{ticker}", run_date)

    log_run(conn, job_type="filings", run_date=run_date, started_at=started, **counts)
    return counts


def main() -> None:
    """Run the daily filings fetch for today's universe."""
    conn = init_db()
    try:
        today = dt.date.today().isoformat()
        universe = get_universe(today)
        counts = run_daily_fetch(
            conn,
            EdgardClient(),
            run_date=today,
            root=ROOT_DIR,
            universe=universe,
        )
        logger.info(f"filings fetch complete: {counts}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
