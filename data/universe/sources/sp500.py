"""S&P 500 constituent source via iShares CSPX fund document (BlackRock API)."""

from __future__ import annotations

import html
import re

import polars as pl
import requests

from data.universe.sources.http import get_with_ca_fallback

CSPX_PORTFOLIO_ID = "253743"
CSPX_API_URL = (
    "https://www.blackrock.com/varnish-api/uk-retail01-product-data/"
    "product-data/api/v1/get-fund-document"
)
USER_AGENT = "marketmind/0.1 (universe maintenance; free data only)"

# iShares uses class-letter suffixes without hyphens; Yahoo requires hyphens.
US_TICKER_OVERRIDES: dict[str, str] = {
    "BRKA": "BRK-A",  # Berkshire Hathaway A
    "BRKB": "BRK-B",  # Berkshire Hathaway B
    "BFA": "BF-A",  # Brown-Forman A
    "BFB": "BF-B",  # Brown-Forman B
}


def normalise_us_ticker(ticker: str) -> str:
    """Map an iShares-style US ticker to the Yahoo Finance symbol.

    Args:
        ticker: Ticker as reported by CSPX (e.g. 'BRKB').

    Returns:
        Yahoo Finance symbol (e.g. 'BRK-B').
    """
    return US_TICKER_OVERRIDES.get(ticker, ticker)


def parse_cspx_holdings(raw: str) -> pl.DataFrame:
    """Parse the CSPX fund document (Excel XML) into equity holdings.

    The document is a SpreadsheetML file with an unescaped '&' in some names,
    so it is parsed tolerantly with row/cell extraction rather than strict XML.

    Args:
        raw: Fund document text as served by the BlackRock API.

    Returns:
        DataFrame with columns [ticker, name, sector, weight, currency],
        equity holdings only, in document order.

    Raises:
        ValueError: If the holdings table or required columns are missing.
    """
    row_blocks = re.findall(r"<ss:Row[^>]*>(.*?)</ss:Row>", raw, re.S)

    def cells(block: str) -> list[str]:
        values = re.findall(r"<ss:Data[^>]*>(.*?)</ss:Data>", block, re.S)
        return [html.unescape(value.strip()) for value in values]

    header_idx: int | None = None
    header: list[str] = []
    for index, block in enumerate(row_blocks):
        values = cells(block)
        if "Issuer Ticker" in values and "Weight (%)" in values:
            header_idx = index
            header = values
            break
    if header_idx is None:
        raise ValueError("CSPX holdings table not found in fund document")

    column_map = {name: index for index, name in enumerate(header)}
    required = {"Issuer Ticker", "Name", "Sector", "Weight (%)", "Market Currency"}
    if not required.issubset(column_map):
        raise ValueError("CSPX holdings table missing required columns")

    rows: list[dict[str, object]] = []
    for block in row_blocks[header_idx + 1 :]:
        values = cells(block)
        if len(values) < len(header):
            continue
        asset_class = values[column_map["Asset Class"]] if "Asset Class" in column_map else ""
        if asset_class != "Equity":
            continue
        ticker = values[column_map["Issuer Ticker"]]
        if not ticker:
            continue
        weight_text = values[column_map["Weight (%)"]].replace(",", "").replace("%", "")
        try:
            weight: float | None = float(weight_text)
        except ValueError:
            weight = None
        rows.append(
            {
                "ticker": ticker,
                "name": values[column_map["Name"]],
                "sector": values[column_map["Sector"]],
                "weight": weight,
                "currency": values[column_map["Market Currency"]],
            }
        )

    if not rows:
        raise ValueError("CSPX holdings table not found in fund document")
    return pl.DataFrame(rows)


def fetch_cspx_holdings(session: requests.Session | None = None) -> pl.DataFrame:
    """Download and parse the current CSPX (S&P 500 UCITS ETF) holdings.

    Args:
        session: Optional requests.Session for connection reuse.

    Returns:
        Parsed holdings DataFrame (see parse_cspx_holdings).

    Raises:
        requests.HTTPError: On non-2xx response.
        ValueError: If the holdings table cannot be parsed.
    """
    resp = get_with_ca_fallback(
        CSPX_API_URL,
        session=session,
        params={
            "appType": "PRODUCT_PAGE",
            "appSubType": "ISHARES",
            "targetSite": "ishares-uk",
            "locale": "en_GB",
            "portfolioId": CSPX_PORTFOLIO_ID,
            "component": "fundDownloadV2",
            "userType": "individual",
        },
        timeout=60,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    return parse_cspx_holdings(resp.text)
