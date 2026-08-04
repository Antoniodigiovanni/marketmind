"""Yahoo Finance lookup and validation for symbol resolution."""

from __future__ import annotations

import requests

from data.universe.sources.http import get_with_ca_fallback

SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "marketmind/0.1 (universe maintenance; free data only)"


def search_by_id(
    id_value: str, *, session: requests.Session | None = None
) -> dict[str, str] | None:
    """Resolve a security identifier (ISIN, CUSIP, or ticker) to a Yahoo symbol.

    Args:
        id_value: ISIN (e.g. 'US0378331005'), CUSIP (e.g. '037833100'), or a
            ticker as fallback.
        session: Optional requests.Session for connection reuse.

    Returns:
        {'symbol': ..., 'name': ...} for the best equity match, or None when
        nothing resolves.

    Raises:
        requests.RequestException: On non-2xx response.
    """
    resp = get_with_ca_fallback(
        SEARCH_URL,
        session=session,
        params={"q": id_value},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    quotes = resp.json().get("quotes") or []
    quote = next((q for q in quotes if q.get("quoteType") == "EQUITY"), None)
    if quote is None and quotes:
        quote = quotes[0]
    if not quote:
        return None
    return {
        "symbol": quote["symbol"],
        "name": quote.get("shortname") or quote.get("longname") or "",
    }


def symbol_has_data(symbol: str, *, session: requests.Session | None = None) -> bool:
    """Check that Yahoo returns recent price bars for a symbol.

    Args:
        symbol: Yahoo Finance symbol.
        session: Optional requests.Session for connection reuse.

    Returns:
        True when the chart endpoint returns timestamps, False otherwise.
    """
    resp = get_with_ca_fallback(
        CHART_URL.format(symbol=symbol),
        session=session,
        params={"range": "5d", "interval": "1d"},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    if resp.status_code != 200:
        return False
    result = (resp.json().get("chart") or {}).get("result") or []
    return bool(result and result[0].get("timestamp"))
