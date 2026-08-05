"""Parse SEC ownership XML (forms 3/4/5) into insider rows."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


def _local(tag: str) -> str:
    """Strip an XML namespace prefix from a tag."""
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _first_text(node: ET.Element, name: str) -> str | None:
    """Return the first descendant text for a local element name."""
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


@dataclass(frozen=True)
class InsiderTransaction:
    """One Form 4/5 transaction row."""

    ticker: str
    owner_name: str
    owner_cik: str | None
    owner_title: str | None
    security_title: str
    transaction_date: str | None
    deemed_execution_date: str | None
    transaction_code: str | None
    shares_transacted: float | None
    price_per_share: float | None
    shares_held_after: float | None
    direct_or_indirect: str | None


@dataclass(frozen=True)
class InsiderHolding:
    """One beneficial-ownership snapshot (forms 3/4/5)."""

    ticker: str
    owner_name: str
    owner_cik: str | None
    owner_title: str | None
    security_title: str
    ref_date: str | None
    shares_held_after: float | None
    direct_or_indirect: str | None
    form_type: str


def _parse_tx(
    node: ET.Element,
    ticker: str,
    owner_cik: str | None,
    owner_name: str,
    owner_title: str | None,
) -> InsiderTransaction:
    title = (
        _first_text(node, "securityTitle")
        or _first_text(node, "underlyingSecurityTitle")
        or "Unknown"
    )
    return InsiderTransaction(
        ticker=ticker,
        owner_name=owner_name,
        owner_cik=owner_cik,
        owner_title=owner_title,
        security_title=title,
        transaction_date=_first_text(node, "transactionDate"),
        deemed_execution_date=_first_text(node, "deemedExecutionDate") or None,
        transaction_code=_first_text(node, "transactionCode"),
        shares_transacted=_num(_first_text(node, "transactionShares")),
        price_per_share=_num(_first_text(node, "transactionPricePerShare")),
        shares_held_after=_num(_first_text(node, "sharesOwnedFollowingTransaction")),
        direct_or_indirect=_first_text(node, "directOrIndirectOwnership"),
    )


def _parse_holding(
    node: ET.Element,
    form_type: str,
    ticker: str,
    owner_cik: str | None,
    owner_name: str,
    owner_title: str | None,
    period: str | None,
) -> InsiderHolding:
    return InsiderHolding(
        ticker=ticker,
        owner_name=owner_name,
        owner_cik=owner_cik,
        owner_title=owner_title,
        security_title=_first_text(node, "securityTitle") or "Unknown",
        ref_date=period,
        shares_held_after=_num(_first_text(node, "sharesOwnedFollowingTransaction")),
        direct_or_indirect=_first_text(node, "directOrIndirectOwnership"),
        form_type=form_type,
    )


def parse_ownership_xml(
    raw: str, *, ticker: str
) -> tuple[list[InsiderTransaction], list[InsiderHolding]]:
    """Parse an SEC ownership XML document (forms 3/4/5).

    Args:
        raw: XML document text.
        ticker: Issuer ticker attached to every row.

    Returns:
        (transactions, holdings) — holdings covers all forms' post-amounts;
        transactions covers form 4/5 trades (non-derivative + derivative).

    Raises:
        xml.etree.ElementTree.ParseError: If raw is not well-formed XML.
    """
    root = ET.fromstring(raw)
    form_type = _first_text(root, "documentType") or ""

    owner_id = _child(root, "reportingOwner")
    if owner_id is None:
        owner_id = root
    owner_cik = _first_text(owner_id, "rptOwnerCik")
    owner_name = _first_text(owner_id, "rptOwnerName") or ""
    relationship = _child(owner_id, "reportingOwnerRelationship")
    if relationship is None:
        relationship = owner_id
    owner_title = _first_text(relationship, "officerTitle")
    if owner_title is None and _first_text(relationship, "isDirector") == "1":
        owner_title = "Director"
    period = _first_text(root, "periodOfReport")

    transactions: list[InsiderTransaction] = []
    holdings: list[InsiderHolding] = []

    non_derivative = _child(root, "nonDerivativeTable")
    if non_derivative is not None:
        for node in non_derivative:
            tag = _local(node.tag)
            if tag == "nonDerivativeTransaction":
                transactions.append(_parse_tx(node, ticker, owner_cik, owner_name, owner_title))
            elif tag == "nonDerivativeHolding":
                holdings.append(
                    _parse_holding(
                        node, form_type, ticker, owner_cik, owner_name, owner_title, period
                    )
                )

    derivative = _child(root, "derivativeTable")
    if derivative is not None:
        for node in derivative:
            tag = _local(node.tag)
            if tag == "derivativeTransaction":
                transactions.append(_parse_tx(node, ticker, owner_cik, owner_name, owner_title))
            elif tag == "derivativeHolding":
                holdings.append(
                    _parse_holding(
                        node, form_type, ticker, owner_cik, owner_name, owner_title, period
                    )
                )

    return transactions, holdings
