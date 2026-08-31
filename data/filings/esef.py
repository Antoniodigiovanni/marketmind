"""ESEF filings backfill into PostgreSQL with raw package archival."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
from pathlib import Path

import polars as pl
import psycopg
from loguru import logger

from data.db.esef import create_esef_filing, esef_filing_exists
from data.db.identifiers import get_current_company_id
from data.db.pool import close_pool, open_pool
from data.sources.xbrlorg import XbrlorgClient
from data.universe.loader import UNIVERSE_DIR

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_fxo(fxo_id: str) -> str:
    return fxo_id.replace("/", "_")


def _iso_date(value: str | None) -> str | None:
    return value[:10] if value else None


def _universe_lei_map(universe_dir: Path, countries: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Map LEI to active universe ticker/country metadata."""
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
    conn: psycopg.AsyncConnection,
    client: XbrlorgClient,
    root: Path,
    *,
    countries: tuple[str, ...] = ("IT", "FR", "ES"),
    universe_dir: Path = UNIVERSE_DIR,
) -> dict[str, int]:
    """Backfill ESEF filings for selected countries into PostgreSQL."""
    counts = {"fetched": 0, "inserted": 0, "skipped": 0, "ignored": 0, "failed": 0}
    lei_map = _universe_lei_map(universe_dir, countries)
    for country in countries:
        for filing in client.get_filings(country):
            counts["fetched"] += 1
            member = lei_map.get(filing.lei)
            if member is None:
                counts["ignored"] += 1
                continue
            if await esef_filing_exists(conn, fxo_id=filing.fxo_id, lei=filing.lei):
                counts["skipped"] += 1
                continue
            try:
                raw = client.download(filing.package_url)
                safe_fxo = _safe_fxo(filing.fxo_id)
                relative = Path("filings-eu") / filing.lei / f"{safe_fxo}.zip"
                output = root / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(raw)
                filed_at = _iso_date(filing.date_added)
                if filed_at is None:
                    raise ValueError(f"ESEF filing {filing.fxo_id} has no filing date")
                period_end = _iso_date(filing.period_end)
                company_id = await get_current_company_id(
                    conn, id_type="ticker", id_value=member["ticker"]
                )
                await create_esef_filing(
                    conn,
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
                    text_path=str(relative),
                    text_hash=_sha256_bytes(raw),
                    size_bytes=len(raw),
                )
                counts["inserted"] += 1
            except Exception:
                logger.exception(f"esef: filing {filing.fxo_id} for {filing.lei} failed")
                counts["failed"] += 1
    return counts


async def _run(client: XbrlorgClient, countries: tuple[str, ...]) -> dict[str, int]:
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            return await backfill_esef(conn, client, ROOT_DIR, countries=countries)
    finally:
        await close_pool(pool)


def main() -> None:
    """Run the ESEF backfill from the command line."""
    parser = argparse.ArgumentParser(description="ESEF filings backfill")
    parser.add_argument("--countries", nargs="+", default=["IT", "FR", "ES"])
    args = parser.parse_args()
    logger.info(asyncio.run(_run(XbrlorgClient(), tuple(args.countries))))


if __name__ == "__main__":
    main()
