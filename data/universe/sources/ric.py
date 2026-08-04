"""Reuters RIC to Yahoo Finance symbol mapping for STOXX constituents."""

from __future__ import annotations

import re

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


def ric_variants(ric: str) -> list[str]:
    """Generate candidate Yahoo symbols for a RIC, most likely first.

    Covers share-class hyphenation (ERICb.ST -> ERIC-B.ST), preference-share
    '_p' suffixes (FPE3_p.DE -> FPE3.DE), and the Irish '.I' -> '.IR' suffix.
    Explicit overrides return only the canonical symbol. Candidates are
    validated against the chart API by the resolution job, never trusted
    blindly.

    Args:
        ric: Reuters instrument code.

    Returns:
        Ordered, de-duplicated candidate list.
    """
    override = RIC_OVERRIDES.get(ric)
    if override:
        return [override]

    candidates = [ric_to_yahoo(ric)]
    base, _, suffix = ric.rpartition(".")

    if base.endswith("_p"):
        candidates.append(f"{base[:-2]}.{suffix}")

    match = re.fullmatch(r"(.+)([a-z])", base)
    if match and not base.endswith("_p") and len(match.group(1)) >= 3:
        candidates.append(f"{match.group(1)}-{match.group(2).upper()}.{suffix}")

    if suffix == "I":
        candidates.append(f"{base}.IR")

    seen: set[str] = set()
    return [candidate for candidate in candidates if not (candidate in seen or seen.add(candidate))]
