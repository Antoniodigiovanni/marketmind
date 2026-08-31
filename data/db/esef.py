"""ESEF/UKSEF filings repository — European annual reports from filings.xbrl.org.

Kept separate from data/db/filings.py because the provider, keys, and
entity identity all differ: EDGAR is CIK/accession-keyed, while ESEF is
LEI/fxo_id-keyed.
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
from psycopg.rows import dict_row


async def create_esef_filing(
    conn: psycopg.AsyncConnection,
    *,
    fxo_id: str,
    lei: str,
    company_id: uuid.UUID | None,
    entity_name: str | None,
    country: str,
    filing_system: str,
    filed_at: dt.date,
    period_end: dt.date | None,
    primary_document: str | None,
    package_url: str,
    xhtml_url: str | None,
    json_url: str | None,
    text_path: str | None,
    text_hash: str | None,
    size_bytes: int | None,
    status: str = "fetched",
) -> uuid.UUID:
    """Insert an ESEF filing metadata row and return its id.

    Args:
        conn: An open connection.
        fxo_id: filings.xbrl.org index id.
        lei: 20-char issuer LEI, as filed.
        company_id: Resolved issuer, or None if it doesn't resolve to a
            tracked company (still worth storing).
        entity_name: Issuer display name, if provided by the source.
        country: ISO country code.
        filing_system: 'ESEF' or 'UKSEF'.
        filed_at: Index date (a market-visibility proxy).
        period_end: Reporting period end, if known.
        primary_document: Name of the primary document within the package.
        package_url: Source URL for the filing package (zip).
        xhtml_url: Optional rendered XHTML URL.
        json_url: Optional structured JSON URL.
        text_path: Path under data/raw/filings-eu/ where the package was
            saved.
        text_hash: sha256 of the downloaded package bytes.
        size_bytes: Size of the downloaded package.
        status: Pipeline status; defaults to 'fetched'.

    Returns:
        The new esef_filing_id.

    Raises:
        psycopg.errors.UniqueViolation: If (fxo_id, lei) already exists.
        psycopg.errors.CheckViolation: If lei is not exactly 20 characters.
    """
    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO esef_filings (fxo_id, lei, company_id, entity_name, country, "
                "filing_system, filed_at, period_end, primary_document, package_url, "
                "xhtml_url, json_url, text_path, text_hash, size_bytes, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING esef_filing_id",
                (
                    fxo_id,
                    lei,
                    company_id,
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
                    status,
                ),
            )
        ).fetchone()
        return row[0]


async def esef_filing_exists(conn: psycopg.AsyncConnection, *, fxo_id: str, lei: str) -> bool:
    """Return True if the ESEF filing is already stored.

    Args:
        conn: An open connection.
        fxo_id: filings.xbrl.org index id.
        lei: Issuer LEI.

    Returns:
        True if present.
    """
    row = await (
        await conn.execute(
            "SELECT 1 FROM esef_filings WHERE fxo_id = %s AND lei = %s", (fxo_id, lei)
        )
    ).fetchone()
    return row is not None


async def get_european_filings(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID | None = None,
    lei: str | None = None,
    country: str | None = None,
    filing_system: str | None = None,
    filed_to: dt.date | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return ESEF filing metadata for European universe members.

    Args:
        conn: An open connection.
        company_id: Optional issuer filter.
        lei: Optional issuer LEI filter.
        country: Optional ISO country code filter (e.g. 'IT', 'FR', 'ES').
        filing_system: Optional filing system filter ('ESEF' or 'UKSEF').
        filed_to: Optional cutoff date; returns filings with
            filed_at <= filed_to.
        limit: Maximum rows.

    Returns:
        One dict per filing: esef_filing_id, fxo_id, lei, company_id,
        entity_name, country, filing_system, filed_at, period_end,
        package_url, xhtml_url, json_url, status, ordered by filed_at
        descending.
    """
    query = (
        "SELECT esef_filing_id, fxo_id, lei, company_id, entity_name, country, "
        "filing_system, filed_at, period_end, package_url, xhtml_url, json_url, status "
        "FROM esef_filings WHERE 1 = 1"
    )
    params: list[object] = []
    if company_id is not None:
        query += " AND company_id = %s"
        params.append(company_id)
    if lei is not None:
        query += " AND lei = %s"
        params.append(lei)
    if country is not None:
        query += " AND country = %s"
        params.append(country)
    if filing_system is not None:
        query += " AND filing_system = %s"
        params.append(filing_system)
    if filed_to is not None:
        query += " AND filed_at <= %s"
        params.append(filed_to)
    query += " ORDER BY filed_at DESC LIMIT %s"
    params.append(limit)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()
