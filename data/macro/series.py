"""Macro series registry — provider, key, name, unit, frequency per series."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MacroSeries:
    """Static metadata for one macro series.

    Attributes:
        id: Short registry id used for filenames and MCP queries (e.g. 'DGS10').
        provider: Source provider — 'fred' or 'ecb'.
        key: Provider series key. FRED: series id. ECB: full SDMX path
            including dataflow (e.g. 'EXR/D.USD.EUR.SP00.A').
        name: Human-readable series name.
        unit: Observation unit ('percent', 'USD per EUR', 'index').
        frequency: 'daily' or 'monthly'.
    """

    id: str
    provider: str
    key: str
    name: str
    unit: str
    frequency: str


MACRO_SERIES: tuple[MacroSeries, ...] = (
    MacroSeries(
        "DGS10", "fred", "DGS10",
        "10-Year Treasury Constant Maturity Rate", "percent", "daily",
    ),
    MacroSeries(
        "BAMLH0A0HYM2", "fred", "BAMLH0A0HYM2",
        "ICE BofA US High Yield OAS", "percent", "daily",
    ),
    MacroSeries(
        "BAMLC0A4CBBB", "fred", "BAMLC0A4CBBB",
        "ICE BofA US BBB OAS", "percent", "daily",
    ),
    MacroSeries(
        "T10Y2Y", "fred", "T10Y2Y",
        "10-Year minus 2-Year Treasury Term Spread", "percent", "daily",
    ),
    MacroSeries(
        "CPIAUCSL", "fred", "CPIAUCSL",
        "CPI All Urban Consumers (SA)", "index", "monthly",
    ),
    MacroSeries(
        "DFF", "fred", "DFF",
        "Effective Federal Funds Rate", "percent", "daily",
    ),
    MacroSeries(
        "VIXCLS", "fred", "VIXCLS",
        "CBOE Volatility Index (Close)", "index", "daily",
    ),
    MacroSeries(
        "EURDFR", "ecb", "FM/D.U2.EUR.4F.KR.DFR.LEV",
        "ECB Deposit Facility Rate", "percent", "daily",
    ),
    MacroSeries(
        "EURUSD", "ecb", "EXR/D.USD.EUR.SP00.A",
        "ECB Reference Exchange Rate USD/EUR", "USD per EUR", "daily",
    ),
    MacroSeries(
        "ESTR", "ecb", "EST/B.EU000A2X2A25.WT",
        "Euro Short-Term Rate (ESTR)", "percent", "daily",
    ),
    MacroSeries(
        "EA10Y", "ecb", "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y",
        "Euro-Area AAA 10-Year Government Yield", "percent", "daily",
    ),
)

SERIES_BY_ID: dict[str, MacroSeries] = {series.id: series for series in MACRO_SERIES}


def get_series(series_id: str) -> MacroSeries | None:
    """Return series metadata by registry id, or None if unknown.

    Args:
        series_id: Registry id (e.g. 'DGS10').

    Returns:
        The matching MacroSeries, or None.
    """
    return SERIES_BY_ID.get(series_id)
