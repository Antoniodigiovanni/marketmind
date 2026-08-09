"""GLEIF LEI search client — free keyless REST API (api.gleif.org)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from data.universe.sources.http import RateLimiter, get_with_ca_fallback

LEI_RECORDS_URL = "https://api.gleif.org/api/v1/lei-records"
USER_AGENT = "marketmind/0.1 (universe maintenance; free data only)"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retryable(exc: BaseException) -> bool:
    """Return True for connection errors and retryable HTTP statuses."""
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in RETRYABLE_STATUS
    return False


@dataclass(frozen=True)
class LeiCandidate:
    """One LEI record candidate from the GLEIF search API."""

    lei: str
    legal_name: str
    country: str
    status: str


class GleifClient:
    """Throttled client for the GLEIF LEI records API."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._limiter = RateLimiter(max_per_second=9.0)

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def search(self, name: str, country: str) -> list[LeiCandidate]:
        """Search LEI records by legal-name text and ISO country code.

        Args:
            name: Entity legal name or instrument name.
            country: ISO 3166-1 alpha-2 country code.

        Returns:
            List of LeiCandidate records (may be empty).

        Raises:
            requests.HTTPError: On non-2xx responses after retries.
        """
        self._limiter.wait()
        resp = get_with_ca_fallback(
            LEI_RECORDS_URL,
            session=self._session,
            params={
                "filter[fulltext]": name,
                "filter[entity.legalAddress.country]": country,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return self._parse(resp.json())

    @staticmethod
    def _parse(payload: dict[str, Any]) -> list[LeiCandidate]:
        """Parse a GLEIF JSON-API page into LeiCandidate records."""
        candidates: list[LeiCandidate] = []
        for item in payload.get("data", []):
            attrs = item.get("attributes", {})
            entity = attrs.get("entity", {})
            candidates.append(
                LeiCandidate(
                    lei=attrs.get("lei", ""),
                    legal_name=entity.get("legalName", ""),
                    country=entity.get("legalAddress", {}).get("country", ""),
                    status=attrs.get("registration", {}).get("status", ""),
                )
            )
        return candidates
