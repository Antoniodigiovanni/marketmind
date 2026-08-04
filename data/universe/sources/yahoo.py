"""Yahoo Finance lookup and validation for symbol resolution."""

from __future__ import annotations

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from data.universe.sources.http import get_with_ca_fallback

SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "marketmind/0.1 (universe maintenance; free data only)"

RETRYABLE = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.SSLError,
)


@retry(
    retry=retry_if_exception_type(RETRYABLE),
    wait=wait_exponential(multiplier=0.5, max=5),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _get(
    url: str,
    *,
    session: requests.Session | None,
    params: dict[str, str],
) -> requests.Response:
    """GET a Yahoo endpoint with retries on transient network errors."""
    return get_with_ca_fallback(
        url, session=session, params=params, timeout=20, headers={"User-Agent": USER_AGENT}
    )


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
    resp = _get(SEARCH_URL, session=session, params={"q": id_value})
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
    resp = _get(
        CHART_URL.format(symbol=symbol),
        session=session,
        params={"range": "5d", "interval": "1d"},
    )
    if resp.status_code != 200:
        return False
    result = (resp.json().get("chart") or {}).get("result") or []
    return bool(result and result[0].get("timestamp"))
