"""Tests for official universe source parsers (STOXX selection list, CSPX holdings)."""

from __future__ import annotations

from pathlib import Path

import certifi
import pytest
import requests

from data.universe.sources.http import SYSTEM_CA_BUNDLE, get_with_ca_fallback
from data.universe.sources.ric import ric_to_yahoo
from data.universe.sources.sp500 import parse_cspx_holdings
from data.universe.sources.stoxx import parse_selection_list

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_selection_list_keeps_only_top_600() -> None:
    raw = (FIXTURES / "stoxx_selection.csv").read_text(encoding="utf-8")
    df = parse_selection_list(raw)

    assert df.columns == ["internal_key", "ric", "name", "country", "exchange", "currency", "rank"]
    assert df.height == 7  # ranks 1..7; 601 and 1228 excluded
    assert df["rank"].max() == 7
    assert "TEST.DE" not in df["ric"].to_list()
    assert df["internal_key"].n_unique() == df.height


def test_parse_selection_list_empty_raises() -> None:
    with pytest.raises(ValueError, match="selection list"):
        parse_selection_list("not,a,valid,file\n1,2,3,4\n")


def test_parse_cspx_holdings_keeps_only_equities() -> None:
    raw = (FIXTURES / "cspx_holdings.xml").read_text(encoding="utf-8")
    df = parse_cspx_holdings(raw)

    assert df.columns == ["ticker", "name", "sector", "weight", "currency"]
    assert df.height == 3  # cash row excluded
    assert "USD CASH" not in df["name"].to_list()
    assert df["ticker"].to_list() == ["NVDA", "AAPL", "JPM"]


def test_parse_cspx_holdings_missing_table_raises() -> None:
    with pytest.raises(ValueError, match="holdings table"):
        parse_cspx_holdings("<ss:Workbook></ss:Workbook>")


def test_ric_to_yahoo_swiss_suffix() -> None:
    assert ric_to_yahoo("NOVN.S") == "NOVN.SW"
    assert ric_to_yahoo("NESN.S") == "NESN.SW"


def test_ric_to_yahoo_identity_suffixes() -> None:
    assert ric_to_yahoo("ASML.AS") == "ASML.AS"
    assert ric_to_yahoo("HSBA.L") == "HSBA.L"
    assert ric_to_yahoo("SAP.DE") == "SAP.DE"
    assert ric_to_yahoo("MC.PA") == "MC.PA"


def test_ric_to_yahoo_known_mismatches() -> None:
    assert ric_to_yahoo("ADSGn.DE") == "ADS.DE"
    assert ric_to_yahoo("MCF.PA") == "MC.PA"
    assert ric_to_yahoo("TTEF.PA") == "TTE.PA"
    assert ric_to_yahoo("TCFP.PA") == "HO.PA"
    assert ric_to_yahoo("SAPG.DE") == "SAP.DE"
    assert ric_to_yahoo("CONG.DE") == "CON.DE"


def test_us_class_shares_are_normalised() -> None:
    from data.universe.sources.sp500 import normalise_us_ticker

    assert normalise_us_ticker("BRKB") == "BRK-B"
    assert normalise_us_ticker("BFB") == "BF-B"
    assert normalise_us_ticker("BRKA") == "BRK-A"
    assert normalise_us_ticker("GOOGL") == "GOOGL"
    assert normalise_us_ticker("NVDA") == "NVDA"


def test_ca_fallback_retries_with_system_bundle() -> None:
    class FakeResponse:
        status_code = 200
        text = "ok"

    calls: list[dict[str, object]] = []

    class FakeSession:
        def get(self, url: str, **kwargs: object) -> FakeResponse:
            calls.append(kwargs)
            if len(calls) == 1:
                raise requests.exceptions.SSLError("certifi failed")
            return FakeResponse()

    response = get_with_ca_fallback("https://example.com", session=FakeSession())
    assert response.status_code == 200
    assert calls[0]["verify"] == certifi.where()
    assert calls[1]["verify"] == SYSTEM_CA_BUNDLE
