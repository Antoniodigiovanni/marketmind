"""Official-website discovery (Wikidata) and bounded same-domain crawling."""

from __future__ import annotations

import html as html_mod
import re
from collections import deque
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

WIKIDATA_CLAIMS_URL = "https://www.wikidata.org/w/api.php"
OFFICIAL_WEBSITE_PROPERTY = "P856"
USER_AGENT = "marketmind/0.1 (company profile enrichment; free data only)"

DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_CHARS_PER_PAGE = 3000
DEFAULT_TOTAL_CHAR_CAP = 12000


def fetch_official_website_url(
    wikibase_item: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 20,
) -> str | None:
    """Resolve the official website (Wikidata property P856) for an item.

    Args:
        wikibase_item: Wikidata item id, e.g. 'Q312'.
        session: Optional requests session (injected in tests).
        timeout: HTTP timeout in seconds.

    Returns:
        The first official website URL, or None when unavailable.
    """
    http = session or requests.Session()
    try:
        response = http.get(
            WIKIDATA_CLAIMS_URL,
            params={
                "action": "wbgetclaims",
                "entity": wikibase_item,
                "property": OFFICIAL_WEBSITE_PROPERTY,
                "format": "json",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
    except requests.RequestException:
        return None

    payload = response.json()
    claims = payload.get("claims", {}).get(OFFICIAL_WEBSITE_PROPERTY, [])
    for claim in claims:
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def _html_to_text(raw: str) -> str:
    """Strip tags/scripts/styles from HTML and collapse whitespace."""
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html_mod.unescape(raw)).strip()


def _fetch_robots(
    session: requests.Session,
    domain: str,
    timeout: int,
) -> RobotFileParser | None:
    """Return a parsed robots.txt for a domain, or None when unavailable."""
    try:
        response = session.get(
            f"https://{domain}/robots.txt",
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
    except requests.RequestException:
        return None
    parser = RobotFileParser()
    parser.parse(response.text.splitlines())
    return parser


def crawl_website(
    start_url: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 20,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars_per_page: int = DEFAULT_MAX_CHARS_PER_PAGE,
    total_char_cap: int = DEFAULT_TOTAL_CHAR_CAP,
) -> list[str]:
    """Crawl a company website breadth-first within the same domain.

    Bounded: respects robots.txt, stays on the start domain, and stops at
    `max_pages` pages or `total_char_cap` characters, each page truncated to
    `max_chars_per_page`.

    Args:
        start_url: Homepage URL.
        session: Optional requests session (injected in tests).
        timeout: HTTP timeout in seconds.
        max_pages: Maximum pages to fetch.
        max_chars_per_page: Character cap per page.
        total_char_cap: Total character cap across all pages.

    Returns:
        List of plain-text page excerpts in crawl order.
    """
    http = session or requests.Session()
    domain = urlparse(start_url).netloc
    robots = _fetch_robots(http, domain, timeout)

    queue: deque[str] = deque([start_url])
    visited: set[str] = set()
    pages: list[str] = []
    total_chars = 0

    while queue and len(pages) < max_pages and total_chars < total_char_cap:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            continue
        try:
            response = http.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
            if response.status_code != 200:
                continue
        except requests.RequestException:
            continue

        text = _html_to_text(response.text)[:max_chars_per_page]
        if not text:
            continue
        pages.append(text)
        total_chars += len(text)

        for href in re.findall(r"""href=["']([^"']+)["']""", response.text, flags=re.I):
            target = urljoin(url, href)
            parsed = urlparse(target)
            if parsed.netloc != domain:
                continue
            if parsed.scheme not in ("http", "https"):
                continue
            if target not in visited:
                queue.append(target)

    return pages
