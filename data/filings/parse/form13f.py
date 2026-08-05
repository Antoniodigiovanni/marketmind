"""Parse SEC 13F-HR cover pages and infotables."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import polars as pl

HOLDINGS_COLUMNS = [
    "report_period",
    "filer_cik",
    "filer_name",
    "issuer_name",
    "class_title",
    "cusip",
    "value_usd",
    "shares",
    "put_call",
    "investment_discretion",
    "other_manager",
    "voting_authority_sole",
    "voting_authority_shared",
    "voting_authority_none",
]

_STRING_COLUMNS = {
    "report_period",
    "filer_cik",
    "filer_name",
    "issuer_name",
    "class_title",
    "cusip",
    "put_call",
    "investment_discretion",
    "other_manager",
}
_SCHEMA = {col: pl.Utf8 if col in _STRING_COLUMNS else pl.Float64 for col in HOLDINGS_COLUMNS}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(node: ET.Element, name: str) -> str | None:
    for child in node.iter():
        if _local(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_13f_cover(raw: str) -> str:
    """Extract the report calendar quarter end from a 13F cover page XML."""
    root = ET.fromstring(raw)
    period = _first_text(root, "reportCalendarOrQuarter")
    if period is None:
        raise ValueError("13F cover page has no reportCalendarOrQuarter")
    return period


def parse_13f_infotable(
    raw: str, *, report_period: str, filer_cik: str, filer_name: str | None
) -> pl.DataFrame:
    """Parse a 13F infotable XML into a holdings DataFrame.

    SEC 13F values are reported in thousands of USD; the returned
    value_usd column is converted to full dollars.

    Args:
        raw: informationTable XML text.
        report_period: Quarter-end date (YYYY-MM-DD).
        filer_cik: Filer CIK (10-digit).
        filer_name: Filer display name, if known.

    Returns:
        DataFrame with HOLDINGS_COLUMNS (empty if no infoTable rows).

    Raises:
        ValueError: If the document has no informationTable element.
    """
    root = ET.fromstring(raw)
    table = next(
        (node for node in root.iter() if _local(node.tag) == "informationTable"),
        None,
    )
    if table is None:
        raise ValueError("13F document has no informationTable element")

    rows: list[dict[str, object]] = []
    for info in table:
        if _local(info.tag) != "infoTable":
            continue
        cusip = _first_text(info, "cusip")
        value = _num(_first_text(info, "value"))
        rows.append(
            {
                "report_period": report_period,
                "filer_cik": filer_cik,
                "filer_name": filer_name,
                "issuer_name": _first_text(info, "nameOfIssuer"),
                "class_title": _first_text(info, "titleOfClass"),
                "cusip": cusip.strip().zfill(9) if cusip else None,
                "value_usd": value * 1000 if value is not None else None,
                "shares": _num(_first_text(info, "sshPrnamt")),
                "put_call": _first_text(info, "putCall"),
                "investment_discretion": _first_text(info, "investmentDiscretion"),
                "other_manager": _first_text(info, "otherManager"),
                "voting_authority_sole": _num(_first_text(info, "Sole")),
                "voting_authority_shared": _num(_first_text(info, "Shared")),
                "voting_authority_none": _num(_first_text(info, "None")),
            }
        )
    return pl.DataFrame(rows, schema=_SCHEMA)
