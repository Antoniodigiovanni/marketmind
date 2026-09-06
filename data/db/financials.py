"""PostgreSQL repository for bitemporal company financial observations."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

import psycopg


@dataclass(frozen=True)
class FinancialObservation:
    """One reported XBRL value with its fiscal and filing-time coordinates."""

    taxonomy: str
    concept: str
    unit: str
    fiscal_year: int | None
    fiscal_period: str | None
    period_start: dt.date | None
    period_end: dt.date
    filed_at: dt.date
    accession_number: str
    form_type: str | None
    frame: str | None
    value: Decimal


async def insert_financials(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    observations: list[FinancialObservation],
) -> int:
    """Insert new financial observations without changing prior filings.

    Args:
        conn: An open PostgreSQL connection.
        company_id: Stable issuer identifier.
        observations: Parsed XBRL observations to append.

    Returns:
        Number of newly inserted rows. Existing provenance-identical rows are
        ignored, making complete Company Facts downloads idempotent.
    """
    if not observations:
        return 0
    params = [
        (
            company_id,
            row.taxonomy,
            row.concept,
            row.unit,
            row.fiscal_year,
            row.fiscal_period,
            row.period_start,
            row.period_end,
            row.filed_at,
            row.accession_number,
            row.form_type,
            row.frame,
            row.value,
        )
        for row in observations
    ]
    async with conn.transaction(), conn.cursor() as cursor:
        await cursor.executemany(
            """
            INSERT INTO financials (
                company_id, taxonomy, concept, unit, fiscal_year, fiscal_period,
                period_start, period_end, filed_at, accession_number, form_type,
                frame, value
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT DO NOTHING
            """,
            params,
        )
        inserted = cursor.rowcount
    return inserted


async def get_financials_as_of(
    conn: psycopg.AsyncConnection,
    *,
    company_id: uuid.UUID,
    concepts: list[str],
    as_of: dt.date,
) -> list[dict[str, object]]:
    """Return the latest visible filing for each concept, unit, and period.

    Args:
        conn: An open PostgreSQL connection.
        company_id: Stable issuer identifier.
        concepts: Exact XBRL local names to return.
        as_of: Filing-date cutoff, inclusive.

    Returns:
        Rows ordered by concept and descending period end. Restatements filed
        after ``as_of`` are excluded, and the newest remaining filing wins
        within each (concept, unit, period_start, period_end) partition.
    """
    if not concepts:
        return []
    result = await conn.execute(
        """
        SELECT taxonomy, concept, unit, fiscal_year, fiscal_period,
               period_start, period_end, filed_at, accession_number,
               form_type, frame, value
        FROM (
            SELECT f.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY taxonomy, concept, unit, period_start, period_end
                       ORDER BY filed_at DESC, accession_number DESC, created_at DESC
                   ) AS observation_rank
            FROM financials AS f
            WHERE company_id = %s
              AND concept = ANY(%s)
              AND filed_at <= %s
        ) AS visible
        WHERE observation_rank = 1
        ORDER BY concept, period_end DESC, period_start DESC NULLS LAST, unit
        """,
        (company_id, concepts, as_of),
    )
    rows = await result.fetchall()
    columns = [
        "taxonomy",
        "concept",
        "unit",
        "fiscal_year",
        "fiscal_period",
        "period_start",
        "period_end",
        "filed_at",
        "accession_number",
        "form_type",
        "frame",
        "value",
    ]
    return [dict(zip(columns, row, strict=True)) for row in rows]
