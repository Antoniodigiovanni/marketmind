"""Parse and ingest SEC Company Facts XBRL observations."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from data.db.financials import FinancialObservation, insert_financials
from data.sources.sec import EdgardClient


def _optional_year(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("fiscal year must be an integer")
    return int(value)


def parse_companyfacts(payload: dict[str, Any]) -> list[FinancialObservation]:
    """Parse an SEC Company Facts document into repository observations.

    Args:
        payload: JSON object returned by the SEC Company Facts endpoint.

    Returns:
        Valid numeric observations across every disclosed taxonomy and concept.
        Entries lacking filing provenance or a period end are omitted because
        they cannot support point-in-time reads.

    Raises:
        ValueError: If the document's required container schema is malformed.
    """
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("SEC Company Facts response has no object-valued 'facts'")

    observations: list[FinancialObservation] = []
    for taxonomy, taxonomy_facts in facts.items():
        if not isinstance(taxonomy, str) or not isinstance(taxonomy_facts, dict):
            raise ValueError("SEC Company Facts taxonomy must map names to concept objects")
        for concept, concept_data in taxonomy_facts.items():
            if not isinstance(concept, str) or not isinstance(concept_data, dict):
                raise ValueError("SEC Company Facts concept must be an object")
            units = concept_data.get("units")
            if not isinstance(units, dict):
                raise ValueError(f"SEC Company Facts concept {taxonomy}:{concept} has no units")
            for unit, entries in units.items():
                if not isinstance(unit, str) or not isinstance(entries, list):
                    raise ValueError(f"SEC Company Facts unit {taxonomy}:{concept} is malformed")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError("SEC Company Facts observation must be an object")
                    if not all(
                        entry.get(key) is not None for key in ("val", "end", "filed", "accn")
                    ):
                        continue
                    try:
                        value = Decimal(str(entry["val"]))
                        if not value.is_finite():
                            continue
                        observations.append(
                            FinancialObservation(
                                taxonomy=taxonomy,
                                concept=concept,
                                unit=unit,
                                fiscal_year=_optional_year(entry.get("fy")),
                                fiscal_period=(
                                    str(entry["fp"]) if entry.get("fp") is not None else None
                                ),
                                period_start=(
                                    dt.date.fromisoformat(str(entry["start"]))
                                    if entry.get("start")
                                    else None
                                ),
                                period_end=dt.date.fromisoformat(str(entry["end"])),
                                filed_at=dt.date.fromisoformat(str(entry["filed"])),
                                accession_number=str(entry["accn"]).replace("-", ""),
                                form_type=(
                                    str(entry["form"]) if entry.get("form") is not None else None
                                ),
                                frame=(
                                    str(entry["frame"]) if entry.get("frame") is not None else None
                                ),
                                value=value,
                            )
                        )
                    except (InvalidOperation, TypeError, ValueError):
                        continue
    return observations


async def ingest_companyfacts(
    conn: psycopg.AsyncConnection,
    client: EdgardClient,
    *,
    company_id: uuid.UUID,
    cik: str,
) -> int:
    """Fetch and append one issuer's complete SEC Company Facts history.

    Args:
        conn: An open PostgreSQL connection.
        client: Courtesy-limited SEC client.
        company_id: Stable issuer identifier.
        cik: Ten-digit, zero-padded SEC Central Index Key.

    Returns:
        Number of observations not already stored.
    """
    payload = client.companyfacts(cik)
    return await insert_financials(
        conn,
        company_id=company_id,
        observations=parse_companyfacts(payload),
    )
