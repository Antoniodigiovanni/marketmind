"""Filings.xbrl.org JSON-API client — ESEF/UKSEF Inline XBRL filings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from data.sources.contact import contact_user_agent
from data.universe.sources.http import RateLimiter, get_with_ca_fallback

FILINGS_URL = "https://filings.xbrl.org/api/filings"
ARTIFACT_BASE_URL = "https://filings.xbrl.org/"
USER_AGENT_PURPOSE = "European filings ingest"
PAGE_SIZE = 200

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retryable(exc: BaseException) -> bool:
    """Return True for connection errors and retryable HTTP statuses."""
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in RETRYABLE_STATUS
    return False


@dataclass(frozen=True)
class EsefFiling:
    """One filing row from the filings.xbrl.org index."""

    fxo_id: str
    lei: str
    entity_name: str
    country: str
    filing_system: str
    period_end: str | None
    date_added: str
    package_url: str
    xhtml_url: str | None
    json_url: str | None
    sha256: str | None


class XbrlorgClient:
    """Throttled JSON-API client for filings.xbrl.org."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = contact_user_agent(USER_AGENT_PURPOSE)
        self._limiter = RateLimiter(max_per_second=2.0)

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get_json(self, url: str) -> dict[str, Any]:
        """GET a JSON-API page with throttling, TLS fallback, and retries."""
        self._limiter.wait()
        resp = get_with_ca_fallback(url, session=self._session, timeout=60)
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def download(self, url: str) -> bytes:
        """Download a raw artifact (e.g. the report package ZIP)."""
        self._limiter.wait()
        resp = get_with_ca_fallback(url, session=self._session, timeout=120)
        resp.raise_for_status()
        return resp.content

    def get_filings(self, country: str, max_pages: int | None = None) -> list[EsefFiling]:
        """Return all index filings for an ISO country, following pagination.

        Args:
            country: ISO 3166-1 alpha-2 country code.
            max_pages: Optional cap on pages fetched (useful for smoke tests).

        Returns:
            List of EsefFiling records.
        """
        url = (
            f"{FILINGS_URL}?filter[country]={country}"
            f"&page[size]={PAGE_SIZE}&include=entity&sort=-processed"
        )
        filings: list[EsefFiling] = []
        pages = 0
        while url:
            payload = self._get_json(url)
            filings.extend(self._parse_page(payload))
            pages += 1
            if max_pages is not None and pages >= max_pages:
                break
            url = payload.get("links", {}).get("next")
        return filings

    @staticmethod
    def _parse_page(payload: dict[str, Any]) -> list[EsefFiling]:
        """Parse one JSON-API page into EsefFiling records."""
        included = {
            item["id"]: item.get("attributes", {})
            for item in payload.get("included", [])
            if item.get("type") == "entity"
        }
        filings: list[EsefFiling] = []
        for item in payload.get("data", []):
            attrs = item.get("attributes", {})
            rel = item.get("relationships", {}).get("entity", {}).get("data", {})
            entity = included.get(rel.get("id"), {})
            country = attrs.get("country", "")
            filings.append(
                EsefFiling(
                    fxo_id=item.get("id", ""),
                    lei=entity.get("identifier", ""),
                    entity_name=entity.get("name", ""),
                    country=country,
                    filing_system="UKSEF" if country == "GB" else "ESEF",
                    period_end=attrs.get("period_end"),
                    date_added=attrs.get("date_added", ""),
                    package_url=urljoin(ARTIFACT_BASE_URL, attrs.get("package_url", "")),
                    xhtml_url=(
                        urljoin(ARTIFACT_BASE_URL, attrs["report_url"])
                        if attrs.get("report_url")
                        else None
                    ),
                    json_url=(
                        urljoin(ARTIFACT_BASE_URL, attrs["json_url"])
                        if attrs.get("json_url")
                        else None
                    ),
                    sha256=attrs.get("sha256"),
                )
            )
        return filings
