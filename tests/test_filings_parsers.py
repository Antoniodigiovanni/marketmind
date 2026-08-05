"""Tests for SEC filing parsers (ownership XML, 13F XML, section chunking)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from data.filings.parse.form13f import parse_13f_cover, parse_13f_infotable
from data.filings.parse.html import html_to_text
from data.filings.parse.ownership import parse_ownership_xml

FIXTURES = Path(__file__).parent / "fixtures" / "sec"


def test_parse_form4_transactions() -> None:
    raw = (FIXTURES / "ownership_form4.xml").read_text(encoding="utf-8")
    transactions, holdings = parse_ownership_xml(raw, ticker="AAPL")

    assert len(transactions) == 2
    sale = transactions[0]
    assert sale.owner_name == "COOK TIMOTHY D"
    assert sale.owner_cik == "0001003781"
    assert sale.owner_title == "Chief Executive Officer"
    assert sale.security_title == "Common Stock"
    assert sale.transaction_code == "S"
    assert sale.transaction_date == "2026-04-30"
    assert sale.shares_transacted == 500.0
    assert sale.price_per_share == 180.25
    assert sale.shares_held_after == 9500.0
    assert sale.direct_or_indirect == "D"

    assert transactions[1].transaction_code == "A"
    assert transactions[1].security_title == "Restricted Stock Unit"
    assert transactions[1].shares_transacted == 120.0


def test_parse_form4_holdings() -> None:
    raw = (FIXTURES / "ownership_form4.xml").read_text(encoding="utf-8")
    transactions, holdings = parse_ownership_xml(raw, ticker="AAPL")

    assert len(holdings) == 1
    assert holdings[0].shares_held_after == 9500.0
    assert holdings[0].ref_date == "2026-05-02"
    assert holdings[0].form_type == "4"


def test_parse_form3_has_no_transactions() -> None:
    raw = (FIXTURES / "ownership_form3.xml").read_text(encoding="utf-8")
    transactions, holdings = parse_ownership_xml(raw, ticker="AAPL")

    assert transactions == []
    assert len(holdings) == 1
    assert holdings[0].form_type == "3"
    assert holdings[0].owner_name == "NEW DIRECTOR"
    assert holdings[0].owner_title == "Director"


def test_parse_ownership_invalid_xml_raises() -> None:
    with pytest.raises(ET.ParseError):
        parse_ownership_xml("not xml", ticker="AAPL")


def test_parse_13f_infotable_rows_and_value_scale() -> None:
    raw = (FIXTURES / "form13f_infotable.xml").read_text(encoding="utf-8")
    df = parse_13f_infotable(
        raw, report_period="2026-03-31", filer_cik="0000123456", filer_name="Test Fund"
    )

    assert df.height == 2
    row = df.row(0, named=True)
    assert row["cusip"] == "037833100"
    assert row["issuer_name"] == "APPLE INC"
    assert row["value_usd"] == 12_345_000.0  # SEC values are in $ thousands
    assert row["shares"] == 10000.0
    assert row["put_call"] is None
    assert row["voting_authority_sole"] == 10000.0

    second = df.row(1, named=True)
    assert second["put_call"] == "PUT"
    assert second["other_manager"] == "5"


def test_parse_13f_infotable_missing_table_raises() -> None:
    with pytest.raises(ValueError, match="informationTable"):
        parse_13f_infotable("<root/>", report_period="2026-03-31", filer_cik="1", filer_name=None)


def test_parse_13f_cover_period() -> None:
    raw = (FIXTURES / "form13f_cover.xml").read_text(encoding="utf-8")
    assert parse_13f_cover(raw) == "2026-03-31"


def test_html_to_text_strips_tags_and_script() -> None:
    raw = (
        "<html><head><title>t</title></head><body>"
        "<script>var x = 1;</script>"
        "<h1>Item 1A</h1><p>Risk factors.</p>"
        "</body></html>"
    )
    text = html_to_text(raw)

    assert "var x" not in text
    assert "Risk factors." in text
    assert "Item 1A" in text
