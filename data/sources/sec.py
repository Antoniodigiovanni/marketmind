"""SEC EDGAR client — official keyless JSON APIs and archive downloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from data.universe.sources.http import RateLimiter, get_with_ca_fallback

USER_AGENT = "marketmind/0.1 (market-data ingest; contact: hermes@example.com)"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retryable(exc: BaseException) -> bool:
    """Return True for connection errors and retryable HTTP statuses."""
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in RETRYABLE_STATUS
    return False


@dataclass(frozen=True)
class Submission:
    """One row from a company's EDGAR submissions history."""

    accession: str
    form_type: str
    filed_at: str
    period_of_report: str | None
    primary_document: str


class EdgardClient:
    """Throttled client for EDGAR JSON endpoints and archive downloads."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._limiter = RateLimiter()

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def get_json(self, url: str) -> Any:
        """GET a JSON endpoint with throttling, TLS fallback, and retries."""
        self._limiter.wait()
        resp = get_with_ca_fallback(url, session=self._session, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def get_text(self, url: str) -> str:
        """GET a raw document with throttling, TLS fallback, and retries."""
        self._limiter.wait()
        resp = get_with_ca_fallback(url, session=self._session, timeout=120)
        resp.raise_for_status()
        return resp.text

    def load_company_tickers(self) -> dict[str, dict[str, str]]:
        """Load the ticker→CIK map from company_tickers.json."""
        data = self.get_json(COMPANY_TICKERS_URL)
        return {
            str(row["ticker"]): {"cik": str(row["cik_str"]).zfill(10), "name": row["title"]}
            for row in data.values()
        }

    def _parse_recent(self, data: dict[str, Any]) -> list[Submission]:
        recent = data["filings"]["recent"]
        return [
            Submission(
                accession=accession.replace("-", ""),
                form_type=form,
                filed_at=filed,
                period_of_report=period or None,
                primary_document=doc,
            )
            for accession, form, filed, period, doc in zip(
                recent["accessionNumber"],
                recent["form"],
                recent["filingDate"],
                recent["reportDate"],
                recent["primaryDocument"],
                strict=True,
            )
        ]

    def submissions(self, cik: str) -> list[Submission]:
        """Return the recent filings history for a CIK."""
        return self._parse_recent(self.get_json(SUBMISSIONS_URL.format(cik=cik)))

    def all_submissions(self, cik: str) -> list[Submission]:
        """Return the complete filing history for a CIK, following pages."""
        data = self.get_json(SUBMISSIONS_URL.format(cik=cik))
        submissions = self._parse_recent(data)
        for entry in data.get("filings", {}).get("files", []):
            page = self.get_json("https://data.sec.gov/submissions/" + entry["name"])
            submissions.extend(self._parse_recent(page))
        return submissions

    def download(self, cik: str, accession: str, doc: str) -> str:
        """Download a raw archive document."""
        url = ARCHIVES_URL.format(cik=cik, accession=accession, doc=doc)
        return self.get_text(url)

    def filing_index(self, cik: str, accession: str) -> list[dict[str, str]]:
        """List files in a filing's archive directory (name + type)."""
        data = self.get_json(INDEX_URL.format(cik=cik, accession=accession))
        return [
            {"name": item["name"], "type": item.get("type", "")}
            for item in data["directory"]["item"]
        ]

    def search_index(
        self,
        *,
        query: str,
        forms: str,
        start: str,
        end: str,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Query the EDGAR full-text search index (paged via cursor)."""
        params: dict[str, str] = {
            "q": query,
            "forms": forms,
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
        }
        if cursor:
            params["cursor"] = cursor
        self._limiter.wait()
        resp = get_with_ca_fallback(SEARCH_URL, session=self._session, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
