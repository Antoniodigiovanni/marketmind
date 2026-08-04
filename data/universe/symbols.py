"""Yahoo Finance symbol mapping for universe constituents."""

from __future__ import annotations

COUNTRY_SUFFIX: dict[str, str] = {
    "Austria": ".VI",
    "Belgium": ".BR",
    "Bermuda": ".L",
    "Czechia": ".PR",
    "Denmark": ".CO",
    "Finland": ".HE",
    "France": ".PA",
    "Germany": ".DE",
    "Greece": ".AT",
    "Hungary": ".BD",
    "Iceland": ".IC",
    "Ireland": ".IR",
    "Israel": ".TA",
    "Italy": ".MI",
    "Luxembourg": ".LU",
    "Netherlands": ".AS",
    "Norway": ".OL",
    "Poland": ".WA",
    "Portugal": ".LS",
    "Spain": ".MC",
    "Sweden": ".ST",
    "Switzerland": ".SW",
    "United Kingdom": ".L",
    "US": "",
}

# Wikipedia local tickers that differ from the Yahoo symbol.
# (ticker, country) -> Yahoo symbol
TICKER_OVERRIDES: dict[tuple[str, str], str] = {
    ("NOVO B", "Denmark"): "NOVO-B.CO",
    ("NOVO C", "Denmark"): "NOVO-C.CO",
    ("MC", "France"): "MC.PA",
    ("TTE", "France"): "TTE.PA",
    ("SHEL", "United Kingdom"): "SHEL.L",
    ("AZN", "United Kingdom"): "AZN.L",
    ("NOV N", "Switzerland"): "NOVN.SW",
    ("SNY", "France"): "SASY.PA",
    ("SASY", "France"): "SASY.PA",
    ("ZG", "Switzerland"): "ZURN.SW",
    ("NDA SE", "Finland"): "NDA-SE.ST",
    ("NDA FI", "Finland"): "NDA-FI.HE",
}


def to_yahoo_symbol(ticker: str, country: str) -> str:
    """Map a Wikipedia ticker + country to a Yahoo Finance symbol.

    The mapping is heuristic: country → exchange suffix, with a curated
    override table for known special cases. Symbols are validated implicitly
    when price fetching begins (roadmap 1.3).

    Args:
        ticker: Local ticker from the source list.
        country: Country of listing as printed by the source.

    Returns:
        Yahoo symbol with exchange suffix (e.g. 'ASML.AS', 'AAPL').
    """
    override = TICKER_OVERRIDES.get((ticker, country))
    if override:
        return override
    return f"{ticker.replace(' ', '-')}{COUNTRY_SUFFIX.get(country, '')}"
