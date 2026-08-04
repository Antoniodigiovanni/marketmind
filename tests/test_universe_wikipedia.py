"""Tests for Wikipedia universe parsing."""

from __future__ import annotations

import pytest

from data.universe.wikipedia import parse_sp500, parse_stoxx600

SP500_HTML = """
<html><body><table>
  <tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
      <th>Headquarters Location</th><th>Date added</th><th>CIK</th><th>Founded</th></tr>
  <tr><td>MMM</td><td>3M</td><td>Industrials</td><td>Industrial Conglomerates</td>
      <td>Saint Paul, Minnesota</td><td>1957-03-04</td><td>0000066740</td><td>1902</td></tr>
  <tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td><td>Technology Hardware</td>
      <td>Cupertino, California</td><td>1980-12-12</td><td>0000320193</td><td>1976</td></tr>
</table></body></html>
"""

STOXX_HTML = """
<html><body><table>
  <tr><th>Ticker</th><th>Company</th><th>ICB Sector</th><th>Country</th><th>Headquarters</th></tr>
  <tr><td>ASML</td><td>ASML Holding</td><td>Technology</td><td>Netherlands</td><td>Veldhoven</td></tr>
  <tr><td>SAP</td><td>SAP SE</td><td>Technology</td><td>Germany</td><td>Walldorf</td></tr>
  <tr><td>SAN</td><td>Sanofi</td><td>Health Care</td><td>France</td><td>Paris</td></tr>
  <tr><td>SAN</td><td>Banco Santander</td><td>Banks</td><td>Spain</td><td>Madrid</td></tr>
  <tr><td>SAP</td><td>SAP SE (dupe)</td><td>Technology</td><td>Germany</td><td>Walldorf</td></tr>
</table></body></html>
"""


def test_parse_sp500() -> None:
    df = parse_sp500(SP500_HTML)
    assert df.columns == ["ticker", "name", "sector", "sub_industry", "country", "added_date"]
    assert df.height == 2
    assert df.row(0) == ("MMM", "3M", "Industrials", "Industrial Conglomerates", "US", "1957-03-04")


def test_parse_sp500_missing_table_raises() -> None:
    with pytest.raises(ValueError, match="constituent table not found"):
        parse_sp500("<html><body><p>nothing here</p></body></html>")


def test_parse_stoxx600_deduplicates_same_ticker_country() -> None:
    df = parse_stoxx600(STOXX_HTML)
    assert df.columns == ["ticker", "name", "sector", "country"]
    assert df.height == 4  # SAP dupe dropped, SAN France + SAN Spain both kept
    names = df.to_dicts()
    assert {r["ticker"] for r in names} == {"ASML", "SAP", "SAN"}
    san_rows = [r for r in names if r["ticker"] == "SAN"]
    assert {r["country"] for r in san_rows} == {"France", "Spain"}


def test_parse_stoxx600_missing_table_raises() -> None:
    with pytest.raises(ValueError, match="constituent table not found"):
        parse_stoxx600("<html><body></body></html>")
