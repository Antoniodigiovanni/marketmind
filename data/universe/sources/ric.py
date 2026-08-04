"""Reuters RIC to Yahoo Finance symbol mapping for STOXX constituents."""

from __future__ import annotations

# Reuters uses '.S' for Swiss listings; Yahoo uses '.SW'.
SWISS_SUFFIX = (".S", ".SW")

# Reuters RICs that differ from the canonical Yahoo symbol for the same
# security. Verified against Yahoo's chart API on 2026-08-04.
RIC_OVERRIDES: dict[str, str] = {
    "ADSGn.DE": "ADS.DE",  # Adidas
    "MCF.PA": "MC.PA",  # LVMH
    "TTEF.PA": "TTE.PA",  # TotalEnergies
    "TCFP.PA": "HO.PA",  # Thales
    "SAPG.DE": "SAP.DE",  # SAP SE
    "CONG.DE": "CON.DE",  # Continental
}


def ric_to_yahoo(ric: str) -> str:
    """Map a Reuters instrument code to the equivalent Yahoo Finance symbol.

    Most RIC suffixes are already Yahoo-native ('.AS', '.L', '.DE', '.PA').
    The main difference is the Swiss suffix ('.S' -> '.SW').

    Args:
        ric: Reuters instrument code, e.g. 'ASML.AS' or 'NOVN.S'.

    Returns:
        Yahoo Finance symbol.
    """
    override = RIC_OVERRIDES.get(ric)
    if override:
        return override
    if ric.endswith(".S") and not ric.endswith(".AS"):
        return ric[:-2] + SWISS_SUFFIX[1]
    return ric
