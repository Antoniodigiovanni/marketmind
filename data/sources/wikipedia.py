"""Company description fetcher via the free Wikipedia APIs."""

from __future__ import annotations

from urllib.parse import quote

import requests

SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
SEARCH_URL = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "marketmind/0.1 (company descriptions; free data only)"


def _get(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int,
) -> requests.Response:
    """GET a Wikipedia endpoint with a browser-like user agent."""
    return session.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )


def _summary_payload(
    session: requests.Session,
    title: str,
    *,
    timeout: int,
) -> dict | None:
    """Fetch the JSON summary payload for a Wikipedia page title.

    Returns None when the page does not exist (404), the extract text when the
    page exists, or "" when the page exists without an extract.
    """
    url = SUMMARY_URL.format(title=quote(title.replace(" ", "_")))
    response = _get(session, url, timeout=timeout)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def fetch_wikipedia_context(
    name: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 20,
) -> dict[str, str | None]:
    """Return the Wikipedia extract, wikibase item, and page URL for a name.

    Tries the REST summary endpoint with the first comma-separated part of the
    company name; on a 404 it falls back to the MediaWiki search API and uses
    the top hit.

    Args:
        name: Company display name from the universe file.
        session: Optional requests session (injected in tests).
        timeout: HTTP timeout in seconds.

    Returns:
        Dict with keys extract (str), wikibase_item (str|None), page_url
        (str|None).
    """
    http = session or requests.Session()
    title = name.split(",")[0].strip()
    payload = _summary_payload(http, title, timeout=timeout)
    if payload is None:
        search = _get(
            http,
            SEARCH_URL,
            params={
                "action": "query",
                "list": "search",
                "srsearch": name,
                "format": "json",
                "srlimit": "1",
            },
            timeout=timeout,
        )
        search.raise_for_status()
        hits = search.json().get("query", {}).get("search", [])
        if not hits:
            return {"extract": "", "wikibase_item": None, "page_url": None}
        payload = _summary_payload(http, hits[0]["title"], timeout=timeout) or {}

    return {
        "extract": payload.get("extract") or "",
        "wikibase_item": payload.get("wikibase_item"),
        "page_url": (payload.get("content_urls") or {}).get("desktop", {}).get("page"),
    }


def fetch_company_description(
    name: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 20,
) -> str:
    """Return a short company description from Wikipedia (keyless).

    Convenience wrapper around fetch_wikipedia_context returning only the
    extract, or "" when no page is found.

    Args:
        name: Company display name from the universe file.
        session: Optional requests session (injected in tests).
        timeout: HTTP timeout in seconds.

    Returns:
        Plain-text extract, or "" if no page is found.
    """
    return fetch_wikipedia_context(name, session=session, timeout=timeout)["extract"] or ""
