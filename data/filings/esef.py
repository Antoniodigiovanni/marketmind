"""ESEF filings backfill — country sweep over filings.xbrl.org.

Dual-written to DuckDB (unchanged) and, additively, to the new PostgreSQL
`esef_filings` table via data.db.esef, keyed by company_id (resolved from
the universe-CSV ticker already looked up for the DuckDB write) rather
than a raw ticker column. See docs/superpowers/plans/
2026-08-11-postgres-filings-cutover.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import uuid
from pathlib import Path

import duckdb
import polars as pl
import psycopg
from loguru import logger

from data.db.esef import create_esef_filing as pg_create_esef_filing
from data.db.identifiers import get_current_company_id
from data.db.init import DEFAULT_DB_PATH, init_db
from data.db.pool import close_pool, open_pool
from data.sources.xbrlorg import XbrlorgClient
from data.universe.loader import UNIVERSE_DIR

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def _sha256_bytes(data: bytes) -> str:
    """Return the sha256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _safe_fxo(fxo_id: str) -> str:
    """Return a filesystem-safe variant of an fxo_id."""
    return fxo_id.replace("/", "_")


def _iso_date(value: str | None) -> str | None:
    """Return the YYYY-MM-DD prefix of a date/datetime string, or None."""
    if not value:
        return None
    return value[:10]


def esef_filing_exists(conn: duckdb.DuckDBPyConnection, fxo_id: str, lei: str) -> bool:
    """Return True if the ESEF filing is already stored."""
    row = conn.execute(
        "SELECT 1 FROM esef_filings WHERE fxo_id = ? AND lei = ?",
        [fxo_id, lei],
    ).fetchone()
    return row is not None


def insert_esef_filing(
    conn: duckdb.DuckDBPyConnection,
    *,
    fxo_id: str,
    lei: str,
    ticker: str | None,
    entity_name: str,
    country: str,
    filing_system: str,
    filed_at: str,
    period_end: str | None,
    primary_document: str,
    package_url: str,
    xhtml_url: str | None,
    json_url: str | None,
    text_path: str,
    text_hash: str,
    size_bytes: int,
) -> str:
    """Insert an ESEF filing metadata row and return its id."""
    filing_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO esef_filings (id, fxo_id, lei, ticker, entity_name, country,
            filing_system, filed_at, period_end, primary_document, package_url,
            xhtml_url, json_url, text_path, text_hash, size_bytes, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fetched')
        """,
        [
            filing_id,
            fxo_id,
            lei,
            ticker,
            entity_name,
            country,
            filing_system,
            filed_at,
            period_end,
            primary_document,
            package_url,
            xhtml_url,
            json_url,
            text_path,
            text_hash,
            size_bytes,
        ],
    )
    return filing_id


def _universe_lei_map(universe_dir: Path, countries: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Map LEI -> {ticker, country} for active members in the given countries."""
    path = universe_dir / "eurostoxx600.csv"
    if not path.exists():
        return {}
    frame = pl.read_csv(path)
    if "lei" not in frame.columns:
        return {}
    frame = frame.filter(
        pl.col("removed_date").is_null()
        & pl.col("country").is_in(list(countries))
        & pl.col("lei").is_not_null()
    )
    return {
        str(row["lei"]): {"ticker": str(row["ticker"]), "country": str(row["country"])}
        for row in frame.to_dicts()
    }


async def backfill_esef(
    conn: duckdb.DuckDBPyConnection,
    pg_conn: psycopg.AsyncConnection,
    client: XbrlorgClient,
    root: Path,
    *,
    countries: tuple[str, ...] = ("IT", "FR", "ES"),
    universe_dir: Path = UNIVERSE_DIR,
) -> dict[str, int]:
    """Backfill ESEF filings for universe members in the given countries.

    Args:
        conn: DuckDB connection -- dedup gate and unchanged store.
        pg_conn: PostgreSQL connection -- additive store (data.db.esef).
        client: filings.xbrl.org client.
        root: Repository root for raw storage.
        countries: ISO country codes to sweep.
        universe_dir: Directory containing eurostoxx600.csv.

    Returns:
        Counts: fetched, inserted, skipped, ignored, failed, unresolved.
    """
    counts = {
        "fetched": 0,
        "inserted": 0,
        "skipped": 0,
        "ignored": 0,
        "failed": 0,
        "unresolved": 0,
    }
    lei_map = _universe_lei_map(universe_dir, countries)
    for country in countries:
        for filing in client.get_filings(country):
            counts["fetched"] += 1
            member = lei_map.get(filing.lei)
            if member is None:
                counts["ignored"] += 1
                continue
            if esef_filing_exists(conn, filing.fxo_id, filing.lei):
                counts["skipped"] += 1
                continue
            try:
                raw = client.download(filing.package_url)
                safe_fxo = _safe_fxo(filing.fxo_id)
                rel_dir = Path("filings-eu") / filing.lei
                out_path = root / rel_dir / f"{safe_fxo}.zip"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(raw)
                filed_at = _iso_date(filing.date_added) or ""
                period_end = _iso_date(filing.period_end)
                text_hash = _sha256_bytes(raw)
                insert_esef_filing(
                    conn,
                    fxo_id=filing.fxo_id,
                    lei=filing.lei,
                    ticker=member["ticker"],
                    entity_name=filing.entity_name,
                    country=filing.country,
                    filing_system=filing.filing_system,
                    filed_at=filed_at,
                    period_end=period_end,
                    primary_document=f"{safe_fxo}.zip",
                    package_url=filing.package_url,
                    xhtml_url=filing.xhtml_url,
                    json_url=filing.json_url,
                    text_path=str(rel_dir / f"{safe_fxo}.zip"),
                    text_hash=text_hash,
                    size_bytes=len(raw),
                )
                company_id = await get_current_company_id(
                    pg_conn, id_type="ticker", id_value=member["ticker"]
                )
                await pg_create_esef_filing(
                    pg_conn,
                    fxo_id=filing.fxo_id,
                    lei=filing.lei,
                    company_id=company_id,
                    entity_name=filing.entity_name,
                    country=filing.country,
                    filing_system=filing.filing_system,
                    filed_at=dt.date.fromisoformat(filed_at),
                    period_end=dt.date.fromisoformat(period_end) if period_end else None,
                    primary_document=f"{safe_fxo}.zip",
                    package_url=filing.package_url,
                    xhtml_url=filing.xhtml_url,
                    json_url=filing.json_url,
                    text_path=str(rel_dir / f"{safe_fxo}.zip"),
                    text_hash=text_hash,
                    size_bytes=len(raw),
                )
                counts["inserted"] += 1
            except Exception:
                logger.exception(f"esef: filing {filing.fxo_id} for {filing.lei} failed; skipped")
                counts["failed"] += 1
    return counts


async def _backfill_esef_with_pool(
    conn: duckdb.DuckDBPyConnection, client: XbrlorgClient, countries: tuple[str, ...]
) -> dict[str, int]:
    """Open a Postgres pool for the duration of one backfill_esef call.

    Bridges the async repository layer into this module's synchronous CLI
    entry point (main()) -- asyncio.run() at that boundary is an
    acceptable bridge for now, matching data.filings.fetch.main().
    """
    pool = await open_pool()
    try:
        async with pool.connection() as pg_conn:
            return await backfill_esef(conn, pg_conn, client, ROOT_DIR, countries=countries)
    finally:
        await close_pool(pool)


def main() -> None:
    """Run the ESEF backfill from the CLI."""
    import asyncio

    parser = argparse.ArgumentParser(description="ESEF filings backfill (filings.xbrl.org)")
    parser.add_argument(
        "--countries",
        nargs="+",
        default=["IT", "FR", "ES"],
        help="ISO country codes",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="DuckDB path")
    args = parser.parse_args()
    conn = init_db(args.db)
    try:
        counts = asyncio.run(_backfill_esef_with_pool(conn, XbrlorgClient(), tuple(args.countries)))
        print(counts)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
