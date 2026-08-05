"""Tests for official universe source parsers (STOXX selection list, CSPX holdings)."""

from __future__ import annotations

from pathlib import Path

import certifi
import pytest
import requests

from data.universe.sources.http import SYSTEM_CA_BUNDLE, get_with_ca_fallback
from data.universe.sources.ric import ric_to_yahoo, ric_variants
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


def test_ric_to_yahoo_verified_override_table() -> None:
    expected = {
        "ADML.L": "ADM.L",  # Admiral
        "AVANZ.ST": "AZA.ST",  # Avanza Bank
        "AZMT.MI": "AZM.MI",  # Azimut
        "BAMN.AS": "BAMNB.AS",  # BAM Groep
        "BKGH.L": "BKG.L",  # Berkeley Group
        "BT.L": "BT-A.L",  # BT Group
        "CAPP.PA": "CAP.PA",  # Capgemini
        "COTNE.SW": "COTN.SW",  # Comet Holdings
        "DAST.PA": "DSY.PA",  # Dassault Systèmes
        "EONGn.DE": "EOAN.DE",  # E.ON
        "FTKn.DE": "FTK.DE",  # Flatexdegiro
        "G1AG.DE": "G1A.DE",  # GEA
        "HMb.ST": "HM-B.ST",  # Hennes & Mauritz B
        "HNKG_p.DE": "HNKG.DE",  # Henkel pref
        "ICAG.L": "IAG.L",  # IAG
        "INGP.WA": "ING.WA",  # ING Bank Slaski
        "ITPG.MI": "IP.MI",  # Interpump
        "KSP.I": "KRX.IR",  # Kingspan
        "PSHG_p.DE": "PAH3.DE",  # Porsche Automobil Holding pref
        "PUBP.PA": "PUB.PA",  # Publicis
        "SABE.MC": "SAB.MC",  # Banco Sabadell
        "SDFGn.DE": "SDF.DE",  # K+S
        "SOGN.PA": "GLE.PA",  # Société Générale
        "SRENH.SW": "SREN.SW",  # Swiss Re
        "VOWG_p.DE": "VOW3.DE",  # Volkswagen pref
        "WDPP.BR": "WDP.BR",  # WDP
        "4HEI.TI": "HEI.DE",  # Heidelberg Materials
        "HBANC.SW": "HBAN.SW",  # Helvetia
        "KYGa.I": "KRZ.IR",  # Kerry Group
        "SJP.L": "STJ.L",  # St James's Place (renamed symbol)
        "SCOR.PA": "SCR.PA",  # SCOR SE
    }
    for ric, symbol in expected.items():
        assert ric_to_yahoo(ric) == symbol


def test_ric_variants_share_class_hyphenation() -> None:
    assert ric_variants("ERICb.ST") == ["ERICb.ST", "ERIC-B.ST"]
    assert ric_variants("SEBa.ST") == ["SEBa.ST", "SEB-A.ST"]
    assert ric_variants("INDUc.ST") == ["INDUc.ST", "INDU-C.ST"]
    assert "HM-B.ST" in ric_variants("HMb.ST")


def test_ric_variants_preference_and_irish_suffix() -> None:
    assert ric_variants("FPE3_p.DE") == ["FPE3_p.DE", "FPE3.DE"]
    assert ric_variants("KSP.I") == ["KRX.IR"]  # verified override


def test_ric_variants_no_extra_candidates_for_clean_symbols() -> None:
    assert ric_variants("ASML.AS") == ["ASML.AS"]
    assert ric_variants("NOVN.S") == ["NOVN.SW"]


def test_ric_variants_override_takes_precedence() -> None:
    assert ric_variants("MCF.PA") == ["MC.PA"]


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
