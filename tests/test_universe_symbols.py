"""Tests for Yahoo symbol mapping."""

from __future__ import annotations

from data.universe.symbols import to_yahoo_symbol


def test_us_ticker_has_no_suffix() -> None:
    assert to_yahoo_symbol("AAPL", "US") == "AAPL"


def test_exchange_suffix_by_country() -> None:
    assert to_yahoo_symbol("ASML", "Netherlands") == "ASML.AS"
    assert to_yahoo_symbol("SAP", "Germany") == "SAP.DE"
    assert to_yahoo_symbol("ZURN", "Switzerland") == "ZURN.SW"
    assert to_yahoo_symbol("ENI", "Italy") == "ENI.MI"
    assert to_yahoo_symbol("HSBA", "United Kingdom") == "HSBA.L"


def test_ticker_overrides_take_precedence() -> None:
    assert to_yahoo_symbol("NOVO B", "Denmark") == "NOVO-B.CO"
    assert to_yahoo_symbol("MC", "France") == "MC.PA"


def test_messy_wikipedia_tickers_are_normalised() -> None:
    assert to_yahoo_symbol("NOV N", "Switzerland") == "NOVN.SW"
    assert to_yahoo_symbol("SNY", "France") == "SASY.PA"
    assert to_yahoo_symbol("SASY", "France") == "SASY.PA"
    assert to_yahoo_symbol("ZG", "Switzerland") == "ZURN.SW"
    assert to_yahoo_symbol("NDA SE", "Finland") == "NDA-SE.ST"
    assert to_yahoo_symbol("NDA FI", "Finland") == "NDA-FI.HE"


def test_share_class_spaces_become_hyphens() -> None:
    assert to_yahoo_symbol("SKA B", "Sweden") == "SKA-B.ST"
    assert to_yahoo_symbol("MAERSK B", "Denmark") == "MAERSK-B.CO"


def test_unknown_country_falls_back_to_plain_ticker() -> None:
    assert to_yahoo_symbol("FOO", "Atlantis") == "FOO"
